import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest

from mm2sniper.config import load_settings
from mm2sniper.marketplaces.base import MarketplaceAdapter
from mm2sniper.marketplaces.bloxswaps import normalize_rows
from mm2sniper.models import Capabilities, ClaimOutcome, ClaimStatus
from mm2sniper.store import Store

FIX = Path(__file__).parent / "fixtures"


def run(coro):
    return asyncio.run(coro)


def payload(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def listings(name: str):
    return normalize_rows(payload(name))[0]


def by_id(ls, listing_id):
    return next(l for l in ls if l.listing_id == listing_id)


def make_settings(tmp_path, mode="monitor", extra_env=None, config=FIX / "watch.toml"):
    env = {"MODE": mode, "DATA_DIR": str(tmp_path / "data"), "LOG_DIR": str(tmp_path / "logs")}
    if mode == "claim":
        env["DISCORD_WEBHOOK_URL"] = "https://discord.com/api/webhooks/1/abc-DEF_123"
        env["BLOXSWAPS_TOKEN"] = "test-token-not-real"
    env.update(extra_env or {})
    return load_settings(config, env=env)


class FakeNotifier:
    enabled = True

    def __init__(self):
        self.events = []
        self.sent = self.failed = self.dropped = 0

    def claim_succeeded(self, listing, outcome):
        self.events.append(("claimed", listing.listing_id, outcome))

    def claim_failed(self, listing, outcome):
        self.events.append(("failed", listing.listing_id, outcome))

    def available(self, listing, mode):
        self.events.append(("available", listing.listing_id, None))

    def system(self, title, message, level="info", key=None):
        self.events.append(("system", title, message))

    def kinds(self):
        return [e[0] for e in self.events]


class FakeAdapter(MarketplaceAdapter):
    """Scriptable adapter: serves fixed listings, records claim calls."""
    name = "bloxswaps"

    def __init__(self, ls=None, *, claim_outcomes=None, claim_states=None, claim_delay=0.0,
                 claim_exc=None, claim=True, status=True):
        self.capabilities = Capabilities(claim=claim, claim_status=status,
                                         claim_unavailable_reason="" if claim else "no token")
        super().__init__()
        self.ls = list(ls or [])
        self.claim_calls = []
        self.claimed = []
        self.claim_outcomes = list(claim_outcomes or [])
        self.claim_states = list(claim_states or [])
        self.claim_delay = claim_delay
        self.claim_exc = list(claim_exc or [])
        self.poll_exc = []

    async def get_listings(self):
        if self.poll_exc:
            raise self.poll_exc.pop(0)
        return list(self.ls)

    async def claim_listing(self, listing, idem_key):
        self.claim_calls.append((listing.listing_id, idem_key))
        self.claimed.append(listing)
        if self.claim_delay:
            await asyncio.sleep(self.claim_delay)
        if self.claim_exc:
            raise self.claim_exc.pop(0)
        if self.claim_outcomes:
            return self.claim_outcomes.pop(0)
        return ClaimOutcome(ClaimStatus.CLAIMED, "claim-1", "ok",
                            server_link="https://www.roblox.com/share?code=abc&type=Server",
                            username="TestAccount", bot_name="boxswap123")

    async def claim_state(self, claim_id, listing):
        return self.claim_states.pop(0) if self.claim_states else ClaimStatus.CLAIMED


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


D = Decimal
