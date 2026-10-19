"""Discord slash commands for the watchlist.

    /watch add item side max min    watch an item, or change its limits
    /watch remove item              stop watching it
    /watch list                     what is watched
    /watch pause | resume           stop or restart sniping (polling pauses too)
    /balance                        BloxSwaps account and balance
    /status                         mode, pause, open claim, last poll

Only the Discord user in DISCORD_MENTION_USER_ID may run them; everyone else is
refused. Replies are ephemeral (only that user sees them). Changes are stored in
the database and apply on the next poll, without a restart or redeploy.

`Commands` holds all logic and knows nothing about Discord, so it is tested
directly. `build_tree` / `start_bot` are the thin discord.py layer.
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal, InvalidOperation
from typing import Literal

from .config import SIDES, Settings, WatchEntry, limit_problems
from .http import AuthError, MarketplaceError
from .models import norm_name
from .notify import _md
from .store import Store
from .watchlist import WatchList

log = logging.getLogger(__name__)
MAX_REPLY = 1900


def _price(value, label: str) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        d = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValueError(f"{label} must be a number") from None
    if not d.is_finite() or d < 0:
        raise ValueError(f"{label} must be a positive number")
    return d


def describe(w: WatchEntry) -> str:
    parts = []
    if w.side in ("withdraw", "both"):
        parts.append(f"withdraw ≤ {w.max_price}" if w.max_price is not None
                     else "withdraw at any price (MAX_CLAIM_PRICE applies)")
    if w.side in ("deposit", "both"):
        parts.append(f"deposit ≥ {w.min_price}" if w.min_price is not None
                     else "deposit at any payout")
    if w.min_qty > 1:
        parts.append(f"at least {w.min_qty} at a time")
    if w.max_qty:
        parts.append(f"at most {w.max_qty} at a time")
    elif w.side in ("deposit", "both"):
        parts.append("deposits 1 at a time (set max_qty to give more)")
    return f"**{_md(w.item)}**: " + ", ".join(parts)


class Commands:
    def __init__(self, settings: Settings, store: Store, adapter, watchlist: WatchList):
        self.s = settings
        self.store = store
        self.adapter = adapter
        self.watchlist = watchlist

    def allowed(self, user_id) -> bool:
        return bool(self.s.discord_owner_id) and str(user_id) == self.s.discord_owner_id

    # ---------------------------------------------------------------- watch

    async def add(self, item: str, side: str = "withdraw", max_price=None, min_price=None,
                  min_qty: int | None = None, max_qty: int | None = None) -> str:
        side = (side or "withdraw").strip().lower()
        if side not in SIDES:
            return f"Side must be one of: {', '.join(SIDES)}."
        item = (item or "").strip()[:100]
        key = norm_name(item)
        if not key:
            return "Give an item name."
        try:
            mx, mn = _price(max_price, "max"), _price(min_price, "min")
        except ValueError as e:
            return f"Not added: {e}."
        if min_qty is not None and min_qty < 1:
            return "Not added: min_qty must be 1 or more."
        if max_qty is not None and max_qty < max(1, min_qty or 1):
            return "Not added: max_qty must be at least 1 and at least min_qty."
        if mx is not None and mx == 0:
            return "Not added: max 0 would never match. Leave it empty for no item limit."
        try:
            listings = await self.adapter.get_listings()
        except MarketplaceError as e:
            return f"Could not reach BloxSwaps to check the name ({e}). Nothing changed."
        hits = [l for l in listings if key in (norm_name(l.name), norm_name(l.slug))]
        note = ""
        if not hits:
            # Not listed yet (a new release, say): watch the name as typed, so it is
            # claimed the moment BloxSwaps adds it.
            words = key.split()
            close = sorted({l.name for l in listings
                            if all(w in norm_name(l.name) for w in words)})[:8]
            hint = (" Similar names listed now: " + ", ".join(_md(n) for n in close) + "."
                    ) if close else ""
            note = (f"\n⚠️ BloxSwaps doesn't list **{_md(item)}** yet. It is watched anyway "
                    f"and claimed when it appears, if the name matches exactly "
                    f"(chromas are \"Chroma <name>\").{hint}")
        name = hits[0].name if hits else item
        entry = WatchEntry(name, side, mx, mn, key=norm_name(name), min_qty=min_qty or 1,
                           max_qty=max_qty)
        if self.s.mode == "claim":
            problems = limit_problems(entry, self.s.max_claim_price)
            if problems:
                return "Not added. " + " ".join(problems)
        existed = any(w.key == entry.key for w in self.store.watch_entries())
        self.store.put_watch(entry)
        now = [f"{l.side} {l.price} ({'in stock' if l.available else 'none now'}"
               f"{', ' + str(l.quantity) if l.available else ''})"
               for l in hits if side in (l.side, "both")]
        return (("Updated " if existed else "Watching ") + describe(entry)
                + (f"\nRight now: {'; '.join(now)}" if now else "")
                + note + "\nApplies on the next poll.")

    async def remove(self, item: str) -> str:
        key = norm_name(item or "")
        for w in self.store.watch_entries():
            if w.key == key or norm_name(w.item) == key:
                self.store.remove_watch(w.key)
                return f"Stopped watching **{_md(w.item)}**."
        return f"**{_md(item)}** is not on the list. Use /watch list."

    def list_text(self) -> str:
        entries = self.store.watch_entries()
        head = "⏸️ Sniping is paused (/watch resume).\n" if self.watchlist.paused else ""
        if not entries:
            return head + "Nothing is watched. Add something with /watch add."
        lines = [f"• {describe(w)}" for w in entries]
        return head + f"Watching {len(entries)} item(s), mode **{self.s.mode}**:\n" \
            + "\n".join(lines)

    def pause(self) -> str:
        self.watchlist.set_paused(True)
        return "⏸️ Paused. Nothing is polled or claimed until /watch resume."

    def resume(self) -> str:
        self.watchlist.set_paused(False)
        return "▶️ Resumed. Watching again from the next poll."

    def item_names(self, current: str, limit: int = 25) -> list[str]:
        """Autocomplete: names from the last poll, filtered by what was typed."""
        want = norm_name(current or "")
        names = (self.adapter.item_names() if hasattr(self.adapter, "item_names")
                 else sorted({l.name for l in getattr(self.adapter, "_cache", [])}))
        hits = [n for n in names if want in norm_name(n)] if want else names
        return hits[:limit]

    def watched_names(self, current: str) -> list[str]:
        want = norm_name(current or "")
        return [w.item for w in self.store.watch_entries() if want in norm_name(w.item)][:25]

    # ---------------------------------------------------------------- info

    async def balance(self) -> str:
        if not self.adapter.capabilities.claim:
            return "No BloxSwaps session: BLOXSWAPS_TOKEN is not set."
        try:
            account = await self.adapter._read_account()
            value = await self.adapter.get_balance()
        except AuthError:
            return "BloxSwaps rejected the session. Paste a fresh BLOXSWAPS_TOKEN."
        except MarketplaceError as e:
            return f"Could not read the balance: {e}"
        return f"Account **{_md(account)}**: {value} BloxSwaps value."

    def status(self) -> str:
        st = self.adapter.status
        hold = self.store.get_hold()
        lines = [f"Mode **{self.s.mode}**, watching {len(self.store.watch_entries())} item(s).",
                 "⏸️ Paused from Discord." if self.watchlist.paused else "Running."]
        if hold:
            lines.append(f"Waiting for the trade of **{_md(hold.name)}** to finish.")
        if st.last_success:
            lines.append(f"Last poll OK: <t:{int(st.last_success.timestamp())}:R>.")
        if st.last_error:
            lines.append(f"Last error: {st.last_error[:200]}")
        return "\n".join(lines)


# -------------------------------------------------------------------- discord.py layer


def _clip(text: str) -> str:
    return text if len(text) <= MAX_REPLY else text[:MAX_REPLY - 1] + "…"


def build_tree(client, commands: Commands, guild=None):
    """Register the slash commands on a new CommandTree. No network."""
    import discord
    from discord import app_commands

    tree = app_commands.CommandTree(client)

    async def guard(interaction: discord.Interaction) -> bool:
        if commands.allowed(interaction.user.id):
            return True
        log.warning("refused a Discord command from user %s", interaction.user.id)
        await interaction.response.send_message("You are not allowed to use this bot.",
                                                ephemeral=True)
        return False

    async def reply(interaction: discord.Interaction, text: str):
        if interaction.response.is_done():
            await interaction.followup.send(_clip(text), ephemeral=True)
        else:
            await interaction.response.send_message(_clip(text), ephemeral=True)

    group = app_commands.Group(name="watch", description="Items the sniper watches")

    @group.command(name="add", description="Watch an item, or change its price limits")
    @app_commands.rename(max_price="max", min_price="min")
    @app_commands.describe(item="BloxSwaps item name",
                           side="withdraw: bots restock it; deposit: a slot opens",
                           max_price="most you pay on a withdraw",
                           min_price="least you accept on a deposit",
                           min_qty="only claim when at least this many units fit",
                           max_qty="most per claim; deposit: how many you have to give "
                                   "(default 1)")
    async def add(interaction: discord.Interaction, item: str,
                  side: Literal["withdraw", "deposit", "both"] = "withdraw",
                  max_price: float | None = None, min_price: float | None = None,
                  min_qty: int | None = None, max_qty: int | None = None):
        if not await guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await reply(interaction, await commands.add(item, side, max_price, min_price,
                                                     min_qty, max_qty))

    @add.autocomplete("item")
    async def add_item(interaction: discord.Interaction, current: str):
        if not commands.allowed(interaction.user.id):
            return []
        return [app_commands.Choice(name=n[:100], value=n[:100])
                for n in commands.item_names(current)]

    @group.command(name="remove", description="Stop watching an item")
    async def remove(interaction: discord.Interaction, item: str):
        if not await guard(interaction):
            return
        await reply(interaction, await commands.remove(item))

    @remove.autocomplete("item")
    async def remove_item(interaction: discord.Interaction, current: str):
        if not commands.allowed(interaction.user.id):
            return []
        return [app_commands.Choice(name=n[:100], value=n[:100])
                for n in commands.watched_names(current)]

    @group.command(name="list", description="Show the watched items")
    async def list_(interaction: discord.Interaction):
        if await guard(interaction):
            await reply(interaction, commands.list_text())

    @group.command(name="pause", description="Pause sniping (no polling, no claims)")
    async def pause(interaction: discord.Interaction):
        if await guard(interaction):
            await reply(interaction, commands.pause())

    @group.command(name="resume", description="Resume sniping")
    async def resume(interaction: discord.Interaction):
        if await guard(interaction):
            await reply(interaction, commands.resume())

    tree.add_command(group, guild=guild)

    @tree.command(name="balance", description="BloxSwaps account and balance", guild=guild)
    async def balance(interaction: discord.Interaction):
        if not await guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await reply(interaction, await commands.balance())

    @tree.command(name="status", description="Sniper status", guild=guild)
    async def status(interaction: discord.Interaction):
        if await guard(interaction):
            await reply(interaction, commands.status())

    return tree


async def start_bot(settings: Settings, commands: Commands, notifier):
    """Connect the bot in the background. A bad token or an outage never stops the
    sniper itself: it is logged and reported through the webhook."""
    import discord

    # Slash commands need no privileged intents; "guilds" only keeps discord.py quiet.
    client = discord.Client(intents=discord.Intents(guilds=True))
    guild = discord.Object(id=int(settings.discord_guild_id)) if settings.discord_guild_id \
        else None
    tree = build_tree(client, commands, guild)

    @client.event
    async def on_ready():
        try:
            synced = await (tree.sync(guild=guild) if guild else tree.sync())
            log.info("discord bot online as %s with %d commands%s", client.user, len(synced),
                     "" if guild else " (global commands can take a few minutes to appear)")
        except Exception as e:  # noqa: BLE001
            log.error("could not register Discord commands: %s", e)

    async def runner():
        try:
            await client.start(settings.discord_bot_token)
        except discord.LoginFailure:
            log.error("Discord rejected DISCORD_BOT_TOKEN")
            notifier.system("Discord bot token rejected",
                            "The /watch commands are off. Check DISCORD_BOT_TOKEN.", "error")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Discord bot stopped")

    client.mm2_task = asyncio.create_task(runner(), name="discord-bot")
    return client
