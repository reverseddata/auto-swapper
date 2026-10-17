"""The long-running watcher.

Loop:
  if a claim is on hold -> check whether it finished / timed out, else wait
  else                  -> poll BloxSwaps, find watched items that just became
                           available (restock / free deposit slot), claim or notify

Failures are contained: a bad poll backs off, a Discord outage only loses
messages, a database error falls back to in-memory dedupe and refuses claims.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import signal
import time
from datetime import datetime, timedelta, timezone

from . import __version__
from .claim import ClaimEngine
from .config import Settings
from .http import AuthError, BadResponse, HttpClient, MarketplaceError, RateLimited
from .lock import InstanceLock
from .marketplaces.base import MarketplaceAdapter
from .models import ClaimStatus, Listing
from .notify import DiscordNotifier
from .store import Change, Store, StoreError, utc_day

log = logging.getLogger(__name__)
DOWN_AFTER = 5            # consecutive failures before a "marketplace down" alert
MAX_BACKOFF = 300.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_trigger(settings: Settings, listing: Listing, change: Change) -> bool:
    """Only a TRANSITION is an opportunity: sold out -> restocked, or full ->
    slot opened. Deposit slots are open most of the time, so "is open" alone
    would claim on every start. A withdraw price drop while in stock also
    counts (it may newly fit max_price). First sightings count only with
    act_on_first_sight."""
    if change is Change.RESTOCKED:
        return True
    if change is Change.NEW:
        return settings.act_on_first_sight
    if change is Change.PRICE_CHANGED:
        return listing.side == "withdraw"
    return False


class Watcher:
    def __init__(self, settings: Settings, store: Store, adapter: MarketplaceAdapter,
                 notifier: DiscordNotifier):
        self.s = settings
        self.store = store
        self.adapter = adapter
        self.notifier = notifier
        self.engine = ClaimEngine(settings, store, adapter, notifier)
        self._stop = asyncio.Event()
        self._down_alerted = False
        self.started_at = _now()

    def stop(self):
        self._stop.set()

    async def _sleep(self, seconds: float) -> bool:
        """Sleep unless asked to stop. Returns True if stopping."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.0, seconds))
            return True
        except asyncio.TimeoutError:
            return False

    # ------------------------------------------------------------ one sweep


    async def process(self, listings: list[Listing]) -> list[tuple[Listing, str]]:
        """Evaluate a sweep. Returns [(listing, action)] for watched items acted on."""
        acted: list[tuple[Listing, str]] = []
        for l in listings:
            change = self.store.observe(l)
            if not is_trigger(self.s, l, change):
                continue
            watch = self.s.watch_for(l.name, l.slug, l.side)
            if watch is None:
                continue
            ok, why = self.engine.eligible(l, watch)
            if not ok:
                log.info("%s (%s) %s but skipped: %s", l.name, l.side, change.value, why)
                continue
            log.info("watched item %s (%s) %s: %s x%d at %s", l.name, l.side, change.value,
                     l.listing_id, l.quantity, l.price)
            if self.s.mode == "monitor":
                self.notifier.available(l, "monitor")
                acted.append((l, "notified"))
                continue
            outcome = await self.engine.attempt(l, watch)
            acted.append((l, outcome.status.value))
            if outcome.status is ClaimStatus.SKIPPED and not outcome.detail.startswith(
                    ("daily cap", "duplicate", "price", "revalidation: price")):
                self.store.mark_unavailable(l.marketplace, l.listing_id)  # retry next poll
            if self.store.get_hold() is not None:
                break  # claimed: stop everything until the trade is done
        return acted

    # ------------------------------------------------------------ main loop

    async def run(self):
        st = self.adapter.status
        self.engine.reconcile_on_startup()
        await self.adapter.connect()
        self.notifier.system(
            "🟢 MM2 sniper started",
            f"v{__version__} · mode **{self.s.mode}** · watching "
            f"{len(self.s.watch)} item(s) every {self.s.poll_interval_seconds:g}s"
            + ("" if self.adapter.capabilities.claim or self.s.mode == "monitor"
               else f"\n⚠️ {self.adapter.capabilities.claim_unavailable_reason}"),
            "ok")
        backoff = self.s.poll_interval_seconds
        while not self._stop.is_set():
            t0 = time.monotonic()
            # ---- hold: no polling until the claim is finished
            try:
                held = await self.engine.check_hold()
            except StoreError as e:
                log.error("hold check failed: %s", e)
                held = True
            if held:
                hold = self.store.get_hold()
                st.paused_reason = f"waiting for trade: {hold.name}" if hold else "hold"
                self.write_status()
                if await self._sleep(self.s.claim_status_interval_seconds):
                    break
                continue
            st.paused_reason = None

            # ---- poll
            st.polls += 1
            try:
                listings = await self.adapter.get_listings()
            except RateLimited as e:
                st.consecutive_failures += 1
                st.last_error = str(e)
                st.rate_limited_until = _now() + timedelta(seconds=e.retry_after)
                wait = max(e.retry_after, 30.0)
                log.warning("bloxswaps rate limited (HTTP %s), pausing %.0fs", e.status, wait)
                self.notifier.system("Rate limited by BloxSwaps",
                                     f"HTTP {e.status}. Pausing {wait:.0f}s, then resuming.",
                                     "warn", key="ratelimit")
                self.write_status()
                if await self._sleep(wait):
                    break
                continue
            except AuthError as e:
                st.consecutive_failures += 1
                st.last_error = str(e)
                self.notifier.system("BloxSwaps refused the item poll (HTTP 401)",
                                     "The item list is public, so this is most likely on "
                                     "BloxSwaps' side. Polling retries with backoff. If claims "
                                     "also report \"could not read session\", paste a fresh "
                                     "BLOXSWAPS_TOKEN.", "error", key="auth")
                if await self._sleep(MAX_BACKOFF):
                    break
                continue
            except (MarketplaceError, BadResponse) as e:
                st.consecutive_failures += 1
                st.last_error = str(e)
                st.reachable = False
                backoff = min(MAX_BACKOFF, max(self.s.poll_interval_seconds, backoff * 2))
                log.warning("poll failed (%d in a row): %s; retrying in %.0fs",
                            st.consecutive_failures, e, backoff)
                if st.consecutive_failures >= DOWN_AFTER and not self._down_alerted:
                    self._down_alerted = True
                    self.notifier.system("🔴 BloxSwaps unreachable",
                                         f"{st.consecutive_failures} failed polls in a row "
                                         f"(latest: {e}). Still retrying.", "error")
                self.write_status()
                if await self._sleep(backoff):
                    break
                continue
            except Exception:  # noqa: BLE001 - a bug in a sweep must not kill the service
                log.exception("unexpected error during poll")
                st.consecutive_failures += 1
                if await self._sleep(min(MAX_BACKOFF, backoff * 2)):
                    break
                continue

            if self._down_alerted:
                self.notifier.system("🟢 BloxSwaps reachable again",
                                     f"Recovered after {st.consecutive_failures} failures.", "ok")
                self._down_alerted = False
            st.reachable, st.last_success, st.last_error = True, _now(), None
            st.consecutive_failures, st.rate_limited_until = 0, None
            st.listings_tracked = len(listings)
            backoff = self.s.poll_interval_seconds
            try:
                await self.process(listings)
            except Exception:  # noqa: BLE001
                log.exception("error while processing listings")
            self.write_status()
            gap = self.s.poll_interval_seconds * random.uniform(0.9, 1.1)
            if await self._sleep(gap - (time.monotonic() - t0)):
                break

    # ------------------------------------------------------------ status

    def status_dict(self) -> dict:
        hold = None
        spent = None
        try:
            h = self.store.get_hold()
            hold = h and {"item": h.name, "since": h.since.isoformat(),
                          "until": h.until.isoformat() if h.until else None}
            spent = str(self.store.spent(utc_day()))
        except StoreError:
            pass
        return {
            "version": __version__,
            "updated_at": _now().isoformat(),
            "started_at": self.started_at.isoformat(),
            "mode": self.s.mode,
            "hold": hold,
            "spent_today": spent,
            "marketplace": self.adapter.status.as_dict(),
            "discord": {"enabled": self.notifier.enabled, "sent": self.notifier.sent,
                        "failed": self.notifier.failed, "dropped": self.notifier.dropped},
        }

    def write_status(self):
        try:
            path = self.s.status_file
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.status_dict(), indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as e:
            log.debug("could not write status file: %s", e)


def build_adapter(settings: Settings, http: HttpClient) -> MarketplaceAdapter:
    from .marketplaces.bloxswaps import BloxSwapsAdapter
    return BloxSwapsAdapter(http, token=settings.bloxswaps_secrets.get("BLOXSWAPS_TOKEN", ""),
                            timeout=settings.request_timeout_seconds,
                            secrets=settings.bloxswaps_secrets,
                            session_path=settings.data_dir / "bloxswaps_session.json")


async def run_service(settings: Settings):
    # One instance per DATA_DIR: two would keep separate claim state, both claim, and
    # fight over the refresh cookie. (Also run only one deployment per token.)
    lock = InstanceLock(settings.data_dir / "mm2sniper.lock")
    if not lock.acquire():
        raise SystemExit(f"another mm2sniper is already running with DATA_DIR="
                         f"{settings.data_dir}; stop it first")
    try:
        await _run_service(settings)
    finally:
        lock.release()


async def _run_service(settings: Settings):
    store = Store(settings.db_path)
    http = HttpClient(min_host_interval=1.0, default_timeout=settings.request_timeout_seconds)
    adapter = build_adapter(settings, http)
    notifier = DiscordNotifier(settings.discord_webhook_url, username=settings.discord_username,
                               cooldown_seconds=settings.alert_cooldown_seconds,
                               mention=settings.discord_mention)
    notifier.start()
    watcher = Watcher(settings, store, adapter, notifier)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, watcher.stop)
        except (NotImplementedError, RuntimeError):  # Windows
            pass
    log.info("starting: %s", settings.redacted())
    try:
        await watcher.run()
    finally:
        log.info("shutting down")
        notifier.system("⏹️ MM2 sniper stopped", "Service shut down.", "info")
        watcher.write_status()
        await notifier.stop()
        await adapter.close()
        await http.close()
        store.close()
