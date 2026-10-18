"""Claim engine: claim a watched item, then hold all polling until the
human finishes the trade (or the claim times out).

Order of operations for one claim (never re-ordered):
  1. refuse if a hold is active (one open claim at a time)
  2. reserve: idempotency key + daily cap, atomically, in SQLite
  3. revalidate: fetch fresh state; still available, same item/side, price OK
  4. claim: one request, bounded by a timeout
  5. outcome:
       CLAIMED  -> store hand-off details, set hold, ping Discord
       REJECTED -> record, alert, keep polling
       UNKNOWN  -> ask the site (reconcile); if still unclear, hold anyway and
                   alert, because a blind retry could claim twice
Only a request that provably never left the machine (RequestNotSent) is retried.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .config import Settings, WatchEntry
from .http import MarketplaceError, RequestNotSent
from .marketplaces.base import MarketplaceAdapter
from .models import ClaimOutcome, ClaimStatus, Listing
from .notify import DiscordNotifier
from .store import Hold, Store, utc_day

log = logging.getLogger(__name__)
SAFE_RETRIES = 2
# The site assigns the bot and server a moment after the claim is created.
HANDOFF_TRIES = 6
HANDOFF_DELAY = 2.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _listing_from_row(row) -> Listing | None:
    """Rebuild enough of a held claim's listing to look for it on the site."""
    try:
        handoff = json.loads(row["handoff"] or "{}")
        return Listing(marketplace=row["marketplace"], listing_id=row["listing_id"],
                       slug=handoff.get("slug") or "", side=row["side"], name=row["name"],
                       price=Decimal(row["price"]), unit=row["unit"], available=True,
                       extra={"id": handoff.get("item_id")})
    except (ValueError, TypeError, KeyError, ArithmeticError):
        return None


class ClaimEngine:
    def __init__(self, settings: Settings, store: Store, adapter: MarketplaceAdapter,
                 notifier: DiscordNotifier):
        self.s = settings
        self.store = store
        self.adapter = adapter
        self.notifier = notifier
        self._lock = asyncio.Lock()
        self._cap_alert_day: str | None = None

    # ------------------------------------------------------------ claim

    def eligible(self, listing: Listing, watch: WatchEntry) -> tuple[bool, str]:
        if not listing.available:
            return False, "not available"
        if not watch.price_ok(listing.side, listing.price):
            return False, f"price {listing.price} outside watch limit"
        if (listing.side == "withdraw" and self.s.max_claim_price is not None
                and listing.price > self.s.max_claim_price):
            return False, f"price {listing.price} above MAX_CLAIM_PRICE"
        return True, ""

    async def attempt(self, listing: Listing, watch: WatchEntry) -> ClaimOutcome:
        async with self._lock:
            if self.store.get_hold() is not None:
                return ClaimOutcome(ClaimStatus.SKIPPED, detail="another claim is on hold")
            if not self.adapter.capabilities.claim:
                return ClaimOutcome(ClaimStatus.SKIPPED,
                                    detail=self.adapter.capabilities.claim_unavailable_reason)
            ok, why = self.eligible(listing, watch)
            if not ok:
                return ClaimOutcome(ClaimStatus.SKIPPED, detail=why)

            # One key per detection: re-submitting the same sighting can never claim twice.
            key = f"{listing.listing_id}:{listing.price}:{listing.detected_at.timestamp():.6f}"
            # Withdraw takes every unit in stock, up to MAX_CLAIM_QUANTITY and the
            # daily cap; a deposit gives one item.
            want = 1
            if listing.side == "withdraw":
                want = max(1, listing.quantity)
                if self.s.max_claim_quantity:
                    want = min(want, self.s.max_claim_quantity)
            res = self.store.reserve_claim(idem_key=key, listing=listing,
                                           daily_limit=self.s.max_daily_spend, quantity=want)
            if not res.ok:
                if res.reason.startswith("daily cap") and self._cap_alert_day != utc_day():
                    self._cap_alert_day = utc_day()
                    self.notifier.system("Daily spend cap reached",
                                         f"Not claiming {listing.name}: {res.reason}. "
                                         "Claims resume tomorrow (UTC).", "warn")
                log.info("claim skipped for %s: %s", listing.name, res.reason)
                return ClaimOutcome(ClaimStatus.SKIPPED, detail=res.reason)

            fresh = await self._revalidate(listing, watch)
            if isinstance(fresh, str):
                self.store.update_claim(key, ClaimStatus.SKIPPED, detail=fresh)
                log.info("claim skipped for %s: %s", listing.name, fresh)
                return ClaimOutcome(ClaimStatus.SKIPPED, detail=fresh)
            fresh = replace(fresh, quantity=await self._quantity(fresh, res.quantity))

            outcome = await self._send(fresh, key)
            if outcome.status is ClaimStatus.UNKNOWN:
                outcome = await self._reconcile(fresh, outcome)
            if outcome.status is ClaimStatus.CLAIMED:
                outcome = await self._complete_handoff(outcome)
            self._finish(fresh, key, outcome)
            if outcome.status is ClaimStatus.CLAIMED:
                self._check_charged(fresh, watch, outcome)
            return outcome

    async def _quantity(self, fresh: Listing, granted: int) -> int:
        """Units to claim: one for a deposit; for a withdraw, what is still in
        stock, within the reservation, and what the site balance can pay for."""
        if fresh.side != "withdraw":
            return 1
        qty = max(1, min(granted, fresh.quantity))
        if qty > 1 and fresh.price > 0 and hasattr(self.adapter, "get_balance"):
            try:
                balance = await asyncio.wait_for(self.adapter.get_balance(),
                                                 self.s.request_timeout_seconds + 5)
                qty = max(1, min(qty, int(balance // fresh.price)))
            except Exception as e:  # noqa: BLE001 - fall back to one unit
                log.warning("balance check failed, claiming 1: %s", e)
                qty = 1
        return qty

    async def _complete_handoff(self, outcome: ClaimOutcome) -> ClaimOutcome:
        """Make sure the claim alert carries the join link. The create answer has no
        bot or server yet, so ask the site for the trade a few times. Never changes
        the claim status: failing here only means the link comes in a later alert."""
        if (outcome.server_link or not outcome.claim_id
                or not getattr(self.adapter, "supports_handoff", False)):
            return outcome
        for attempt in range(HANDOFF_TRIES):
            try:
                link, bot = await asyncio.wait_for(self.adapter.fetch_handoff(outcome.claim_id),
                                                   self.s.request_timeout_seconds + 5)
            except Exception as e:  # noqa: BLE001 - the claim itself already succeeded
                log.debug("hand-off fetch failed: %s", e)
                link = bot = ""
            if link:
                return replace(outcome, server_link=link, bot_name=bot or outcome.bot_name)
            if attempt + 1 < HANDOFF_TRIES:
                await asyncio.sleep(HANDOFF_DELAY)
        log.warning("claim %s has no join link yet; it will follow", outcome.claim_id)
        return outcome

    async def _follow_up_handoff(self, row):
        """A claim whose alert went out without a join link: send the link once known."""
        try:
            handoff = json.loads(row["handoff"] or "{}")
        except ValueError:
            return
        if handoff.get("server_link") or not row["claim_id"] \
                or not getattr(self.adapter, "supports_handoff", False):
            return
        try:
            link, bot = await self.adapter.fetch_handoff(row["claim_id"])
        except Exception as e:  # noqa: BLE001
            log.debug("hand-off follow-up failed: %s", e)
            return
        listing = _listing_from_row(row)
        if not link or listing is None:
            return
        handoff.update(server_link=link, bot_name=bot)
        self.store.update_claim(row["idem_key"], ClaimStatus.CLAIMED, detail=row["detail"] or "",
                                handoff=handoff)
        self.notifier.claim_succeeded(listing, ClaimOutcome(
            ClaimStatus.CLAIMED, row["claim_id"], "join link ready", server_link=link,
            username=self.adapter.account, bot_name=bot))

    def _check_charged(self, listing: Listing, watch: WatchEntry, outcome: ClaimOutcome):
        """The claim request cannot lock a price. If the trade the site created is
        outside the limits, say so loudly: cancel it on BloxSwaps before trading."""
        charged = outcome.charged
        if charged is None:
            return
        problems = []
        if listing.side == "withdraw":
            qty = max(1, listing.quantity)
            each = charged / qty
            if charged > listing.price * qty:
                problems.append(f"costs {charged} for {qty}, listed at {listing.price} each")
            if watch.max_price is not None and each > watch.max_price:
                problems.append(f"above max_price {watch.max_price}")
            if self.s.max_claim_price is not None and each > self.s.max_claim_price:
                problems.append(f"above MAX_CLAIM_PRICE {self.s.max_claim_price}")
        else:
            if charged < listing.price:
                problems.append(f"pays {charged}, listed at {listing.price}")
            if watch.min_price is not None and charged < watch.min_price:
                problems.append(f"below min_price {watch.min_price}")
        if problems:
            log.warning("claimed %s outside limits: %s", listing.name, "; ".join(problems))
            self.notifier.system(f"⚠️ {listing.name}: price moved",
                                 "The trade was created but " + "; ".join(problems)
                                 + ". Cancel it on BloxSwaps if you don't want it.", "error")

    async def _revalidate(self, listing: Listing, watch: WatchEntry) -> Listing | str:
        try:
            fresh = await self.adapter.get_listing_details(listing.listing_id)
        except MarketplaceError as e:
            return f"revalidation failed: {e}"
        if fresh is None:
            return "listing disappeared"
        if fresh.slug != listing.slug or fresh.side != listing.side:
            return "listing identity changed"
        ok, why = self.eligible(fresh, watch)
        return fresh if ok else f"revalidation: {why}"

    async def _send(self, listing: Listing, key: str) -> ClaimOutcome:
        for attempt in range(SAFE_RETRIES + 1):
            try:
                return await asyncio.wait_for(self.adapter.claim_listing(listing, key),
                                              self.s.request_timeout_seconds + 5)
            except RequestNotSent as e:
                if attempt == SAFE_RETRIES:
                    return ClaimOutcome(ClaimStatus.REJECTED, detail=f"could not connect: {e}")
                await asyncio.sleep(1 + attempt)
            except asyncio.TimeoutError:
                return ClaimOutcome(ClaimStatus.UNKNOWN, detail="claim request timed out")
            except Exception as e:  # noqa: BLE001 - outcome unknown, must not retry
                log.exception("claim request raised")
                return ClaimOutcome(ClaimStatus.UNKNOWN, detail=f"{type(e).__name__}: {e}")
        return ClaimOutcome(ClaimStatus.UNKNOWN, detail="unreachable")

    async def _reconcile(self, listing: Listing, outcome: ClaimOutcome) -> ClaimOutcome:
        try:
            state, found_id = await self.adapter.reconcile_claim(listing)
        except Exception as e:  # noqa: BLE001
            log.warning("reconcile failed: %s", e)
            return outcome
        if state is ClaimStatus.CLAIMED:
            # Keep the trade id, so the hold can follow the trade to completion.
            return ClaimOutcome(ClaimStatus.CLAIMED, found_id or outcome.claim_id,
                                "confirmed on reconcile (no hand-off details in response)",
                                username=self.adapter.account)
        if state in (ClaimStatus.REJECTED, ClaimStatus.EXPIRED):
            return ClaimOutcome(ClaimStatus.REJECTED, outcome.claim_id,
                                f"reconcile: site shows no claim ({outcome.detail})")
        return outcome

    def _finish(self, listing: Listing, key: str, outcome: ClaimOutcome):
        if outcome.status in (ClaimStatus.CLAIMED, ClaimStatus.UNKNOWN):
            if not outcome.username and self.adapter.account:
                outcome = ClaimOutcome(**{**outcome.__dict__,
                                          "username": self.adapter.account})
            handoff = {"server_link": outcome.server_link, "username": outcome.username,
                       "bot_name": outcome.bot_name, "listing_id": listing.listing_id,
                       "slug": listing.slug, "side": listing.side,
                       "item_id": listing.extra.get("id")}
            self.store.update_claim(key, outcome.status, claim_id=outcome.claim_id,
                                    detail=outcome.detail, handoff=handoff)
            mins = self.s.claim_hold_timeout_minutes
            self.store.set_hold(Hold(key, listing.name, _now(),
                                     _now() + timedelta(minutes=mins) if mins > 0 else None))
            if outcome.status is ClaimStatus.CLAIMED:
                log.info("CLAIMED %s (%s) at %s", listing.name, listing.side, listing.price)
                self.notifier.claim_succeeded(listing, outcome)
            else:
                log.warning("claim outcome UNKNOWN for %s: %s", listing.name, outcome.detail)
                self.notifier.claim_failed(listing, outcome)
        else:
            self.store.update_claim(key, outcome.status, claim_id=outcome.claim_id,
                                    detail=outcome.detail)
            log.info("claim %s for %s: %s", outcome.status.value, listing.name, outcome.detail)
            self.notifier.claim_failed(listing, outcome)

    # ------------------------------------------------------------ hold

    async def check_hold(self) -> bool:
        """Resume polling if the held claim finished or timed out.
        Returns True if a hold is still active afterwards."""
        hold = self.store.get_hold()
        if hold is None:
            return False
        row = self.store.claim(hold.idem_key)
        if hold.until is not None and _now() >= hold.until:
            # Before resuming, ask the site. A trade it still shows as open keeps the
            # hold: resuming then could put a second live trade next to it.
            try:
                state = await self._site_state(row)
            except Exception as e:  # noqa: BLE001
                log.debug("claim status check at timeout failed: %s", e)
                state = ClaimStatus.UNKNOWN
            if state is ClaimStatus.CLAIMED:
                self.notifier.system("⏳ Claim still open",
                                     f"The trade for {hold.name} is still open on BloxSwaps, "
                                     "so polling stays paused. Finish or cancel it there, or "
                                     "run `mm2sniper resume`.", "warn",
                                     key=f"stillopen:{hold.idem_key}")
                return True
            if state in (ClaimStatus.COMPLETED, ClaimStatus.EXPIRED):
                self.store.update_claim(hold.idem_key, state, detail=f"site reports {state.value}")
                self.resume(f"trade for {hold.name} {state.value}",
                            level="ok" if state is ClaimStatus.COMPLETED else "warn")
                return False
            if row is not None:
                # No final answer: it may still be traded, so it keeps counting as spent.
                self.store.update_claim(hold.idem_key, ClaimStatus.TIMED_OUT,
                                        detail="hold timed out without a final answer")
            self.resume(f"claim for {hold.name} timed out after "
                        f"{self.s.claim_hold_timeout_minutes:g} min; check BloxSwaps",
                        level="warn")
            return False
        try:
            state = await self._site_state(row)
        except Exception as e:  # noqa: BLE001
            log.debug("claim status check failed: %s", e)
            return True
        if state in (ClaimStatus.COMPLETED, ClaimStatus.EXPIRED):
            self.store.update_claim(hold.idem_key, state, detail=f"site reports {state.value}")
            self.resume(f"trade for {hold.name} {state.value}",
                        level="ok" if state is ClaimStatus.COMPLETED else "warn")
            return False
        if state is ClaimStatus.CLAIMED:
            await self._follow_up_handoff(self.store.claim(hold.idem_key))
        return True

    async def _site_state(self, row) -> ClaimStatus:
        """Where the held claim stands on the site. With a trade id, read it. Without
        one (the claim answer was unclear), look for it among open trades, and keep
        its id once found, so a claim that did go through is followed, not forgotten."""
        if row is None or not self.adapter.capabilities.claim_status:
            return ClaimStatus.UNKNOWN
        if row["claim_id"]:
            return await self.adapter.claim_state(row["claim_id"], None)
        listing = _listing_from_row(row)
        if listing is None:
            return ClaimStatus.UNKNOWN
        state, found_id = await self.adapter.reconcile_claim(listing)
        if state is ClaimStatus.CLAIMED:
            self.store.update_claim(row["idem_key"], ClaimStatus.CLAIMED, claim_id=found_id,
                                    detail="found on the site after an unclear answer")
        return state

    def resume(self, reason: str, level: str = "ok"):
        self.store.clear_hold()
        log.info("polling resumed: %s", reason)
        self.notifier.system("▶️ Polling resumed", reason, level)

    def reconcile_on_startup(self):
        """A PENDING row at startup means we crashed mid-claim: outcome unknown."""
        for row in self.store.open_claims():
            if row["status"] == ClaimStatus.PENDING.value:
                self.store.update_claim(row["idem_key"], ClaimStatus.UNKNOWN,
                                        detail="process stopped mid-claim")
                if self.store.get_hold() is None:
                    mins = self.s.claim_hold_timeout_minutes
                    self.store.set_hold(Hold(row["idem_key"], row["name"], _now(),
                                             _now() + timedelta(minutes=mins) if mins else None))
                self.notifier.system(
                    "⚠️ Unfinished claim found at startup",
                    f"A claim for {row['name']} was in flight when the process stopped. "
                    "Check BloxSwaps; polling stays paused until it finishes, times out, "
                    "or you run `mm2sniper resume`.", "warn")
