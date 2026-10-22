"""Discord watchlist commands: logic, authorization, persistence and live reload.
No Discord connection is made."""
import asyncio
from decimal import Decimal

import pytest

from conftest import FakeAdapter, FakeNotifier, listings, make_settings, run
from mm2sniper.config import ConfigError, WatchEntry, load_settings
from mm2sniper.discord_bot import Commands, build_tree
from mm2sniper.http import AuthError, TransientError
from mm2sniper.service import Watcher
from mm2sniper.store import Store
from mm2sniper.watchlist import WatchList

D = Decimal
OWNER = "123456789"


def settings(tmp_path, mode="claim", **env):
    base = {"DISCORD_BOT_TOKEN": "bot-token-not-real-123", "DISCORD_MENTION_USER_ID": OWNER}
    base.update(env)
    return make_settings(tmp_path, mode, extra_env=base)


def setup(tmp_path, mode="claim", ls=None, **env):
    s = settings(tmp_path, mode, **env)
    store = Store(":memory:")
    a = FakeAdapter(ls if ls is not None else listings("bloxswaps_after.json"))
    wl = WatchList(s, store)
    return s, store, a, wl, Commands(s, store, a, wl)


# ---------------------------------------------------------------- config


def test_bot_token_needs_an_owner(tmp_path):
    with pytest.raises(ConfigError) as e:
        make_settings(tmp_path, "monitor", extra_env={"DISCORD_BOT_TOKEN": "x" * 20})
    assert "DISCORD_MENTION_USER_ID" in " ".join(e.value.problems)


def test_empty_watchlist_is_fine_with_a_bot(tmp_path):
    env = {"MODE": "monitor", "DATA_DIR": str(tmp_path), "LOG_DIR": str(tmp_path),
           "DISCORD_BOT_TOKEN": "x" * 20, "DISCORD_MENTION_USER_ID": OWNER}
    s = load_settings(None, env=env)
    assert s.watch == [] and s.discord_bot_token


def test_bot_token_never_printed(tmp_path):
    s = settings(tmp_path)
    assert "bot-token-not-real-123" not in repr(s)
    assert "bot-token-not-real-123" not in str(s.redacted())
    from mm2sniper.notify import redact
    assert "bot-token-not-real-123" not in redact("token is bot-token-not-real-123")


# ---------------------------------------------------------------- store / watchlist


def test_config_entries_seed_the_database_once(tmp_path):
    s, store, a, wl, c = setup(tmp_path)
    assert {w.item for w in wl.entries()} == {w.item for w in s.watch}
    store.remove_watch(wl.entries()[0].key)
    left = len(wl.entries())
    WatchList(s, store)                       # a restart must not re-add removed items
    assert len(wl.entries()) == left


def test_without_a_bot_the_config_list_is_used(tmp_path):
    s = make_settings(tmp_path, "claim")
    store = Store(":memory:")
    wl = WatchList(s, store)
    assert not wl.managed and wl.entries() == s.watch
    assert store.watch_entries() == []


def test_watch_entries_round_trip(store):
    w = WatchEntry("Harvester", "both", D("330.5"), D("10"), key="harvester")
    store.put_watch(w)
    assert store.watch_entries() == [w]
    assert store.remove_watch("harvester") and not store.remove_watch("harvester")


# ---------------------------------------------------------------- commands


def test_add_validates_against_bloxswaps_and_persists(tmp_path):
    s, store, a, wl, c = setup(tmp_path)
    msg = run(c.add("harvester", "withdraw", 350))
    assert msg.startswith("Updated") and "350" in msg      # seeded already: limits change
    w = next(w for w in store.watch_entries() if w.key == "harvester")
    assert w.max_price == D(350)


def test_add_unlisted_item_is_watched_for_release(tmp_path):
    s, store, a, wl, c = setup(tmp_path)
    msg = run(c.add("Chroma Mortal", "withdraw", 5, min_qty=2))
    assert "doesn't list" in msg
    w = next(w for w in store.watch_entries() if w.key == "chroma mortal")
    assert w.item == "Chroma Mortal" and w.max_price == D(5) and w.min_qty == 2


def test_add_refuses_unlimited_entries_in_claim_mode(tmp_path):
    s, store, a, wl, c = setup(tmp_path)
    assert "Not added" in run(c.add("Harvester", "withdraw"))
    assert "Not added" in run(c.add("Harvester", "deposit"))
    assert "Not added" in run(c.add("Harvester", "withdraw", 0))
    assert "Not added" in run(c.add("Harvester", "withdraw", -1))
    assert "Side must be" in run(c.add("Harvester", "sideways", 5))


def test_add_allows_no_limit_in_monitor_mode(tmp_path):
    s, store, a, wl, c = setup(tmp_path, mode="monitor")
    assert "Not added" not in run(c.add("Harvester", "withdraw"))


def test_add_when_bloxswaps_is_down_changes_nothing(tmp_path):
    s, store, a, wl, c = setup(tmp_path)
    a.poll_exc = [TransientError("down")]
    before = store.watch_entries()
    assert "Nothing changed" in run(c.add("Harvester", "withdraw", 5))
    assert store.watch_entries() == before


def test_remove_list_pause_resume(tmp_path):
    s, store, a, wl, c = setup(tmp_path)
    assert "Stopped watching" in run(c.remove("harvester"))
    assert "not on the list" in run(c.remove("harvester"))
    assert "Harvester" not in c.list_text()
    assert "Paused" in c.pause() and wl.paused and "paused" in c.list_text().lower()
    assert "Resumed" in c.resume() and not wl.paused


def test_names_are_markdown_escaped_in_replies(tmp_path):
    s, store, a, wl, c = setup(tmp_path)
    msg = run(c.remove("[x](https://evil.example)"))
    assert "](https://evil.example)" not in msg


def test_balance_reports_session_problems(tmp_path):
    s, store, a, wl, c = setup(tmp_path)

    async def bad():
        raise AuthError("401")
    a._read_account = bad
    assert "rejected the session" in run(c.balance())


def test_only_the_owner_is_allowed(tmp_path):
    s, store, a, wl, c = setup(tmp_path)
    assert c.allowed(OWNER) and c.allowed(int(OWNER))
    assert not c.allowed("999") and not c.allowed("")


# ---------------------------------------------------------------- live reload + pause


def test_watchlist_change_applies_on_next_sweep(tmp_path):
    s, store, a, wl, c = setup(tmp_path, mode="monitor")
    before = listings("bloxswaps_before.json")
    after = listings("bloxswaps_after.json")
    n = FakeNotifier()
    w = Watcher(s, store, a, n)
    for key in [x.key for x in store.watch_entries()]:
        store.remove_watch(key)              # nothing watched
    run(w.process(before))
    assert run(w.process(after)) == []
    run(c.add("Harvester", "withdraw"))      # added from Discord, no restart
    restocked = [l for l in after if l.listing_id == "harvester:withdraw"]
    store.mark_unavailable("bloxswaps", "harvester:withdraw")
    acted = run(w.process(restocked))
    assert [l.listing_id for l, _ in acted] == ["harvester:withdraw"]


def test_pause_stops_polling(tmp_path):
    s, store, a, wl, c = setup(tmp_path, mode="monitor")
    calls = []
    orig = a.get_listings

    async def counted():
        calls.append(1)
        return await orig()
    a.get_listings = counted
    w = Watcher(s, store, a, FakeNotifier())
    w.watchlist.set_paused(True)

    async def go():
        task = asyncio.create_task(w.run())
        await asyncio.sleep(0.2)
        w.stop()
        await task
    run(go())
    assert calls == [] and a.status.paused_reason.startswith("paused from Discord")


# ---------------------------------------------------------------- discord.py layer


def test_slash_commands_are_registered_and_guarded(tmp_path):
    import discord
    s, store, a, wl, c = setup(tmp_path)

    async def go():
        client = discord.Client(intents=discord.Intents.none())
        tree = build_tree(client, c)
        names = {cmd.name for cmd in tree.get_commands()}
        group = tree.get_command("watch")
        sub = {cmd.name for cmd in group.commands}

        sent = []

        class Response:
            def is_done(self):
                return bool(sent)

            async def send_message(self, text, ephemeral=False):
                sent.append((text, ephemeral))

            async def defer(self, **kw):
                sent.append(("<defer>", True))

        class Followup:
            async def send(self, text, ephemeral=False):
                sent.append((text, ephemeral))

        class Interaction:
            def __init__(self, uid):
                self.user = type("U", (), {"id": uid})()
                self.response = Response()
                self.followup = Followup()

        await group.get_command("list").callback(Interaction(999))
        stranger = list(sent)
        sent.clear()
        await group.get_command("list").callback(Interaction(int(OWNER)))
        owner = list(sent)
        await client.close()
        return names, sub, stranger, owner

    names, sub, stranger, owner = asyncio.run(go())
    assert names == {"watch", "balance", "status", "market"}
    assert sub == {"add", "remove", "list", "pause", "resume"}
    assert stranger == [("You are not allowed to use this bot.", True)]
    assert owner and owner[0][1] is True and "Watching" in owner[0][0]
