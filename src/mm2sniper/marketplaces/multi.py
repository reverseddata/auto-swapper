"""Several marketplaces behind one adapter.

Polls every market that is switched on (MARKETS, or /market in Discord) and merges
their listings. One market failing never stops the others: its error is kept in
`errors` and only raised when every enabled market failed.

Claims, claim status and the session all go to BloxSwaps, the only market that can
claim. `can_claim(listing)` tells the service whether a match is claimed or only
alerted.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from ..models import ClaimOutcome, ClaimStatus, Listing, MarketplaceStatus
from .base import MarketplaceAdapter

log = logging.getLogger(__name__)
KNOWN = ("bloxswaps", "starpets")


class MultiMarketAdapter(MarketplaceAdapter):
    name = "markets"
    supports_only = True
    supports_handoff = True

    def __init__(self, adapters: dict[str, MarketplaceAdapter],
                 enabled: Callable[[], set[str]]):
        self.adapters = adapters
        self.primary = adapters["bloxswaps"]
        self.capabilities = self.primary.capabilities
        self.enabled = enabled
        super().__init__()
        self.status = MarketplaceStatus("markets", self.capabilities)
        self.errors: dict[str, str] = {}

    # ---- the claiming market's identity

    @property
    def account(self) -> str:
        return self.primary.account

    @account.setter
    def account(self, value: str):  # base class assigns a default
        pass

    def can_claim(self, listing: Listing) -> bool:
        a = self.adapters.get(listing.marketplace)
        return a is not None and a.capabilities.claim

    def active(self) -> list[str]:
        return [m for m in KNOWN if m in self.enabled() and m in self.adapters]

    # ---- monitoring

    async def connect(self):
        for a in self.adapters.values():
            await a.connect()

    async def close(self):
        for a in self.adapters.values():
            await a.close()

    async def get_listings(self, only: set[str] | None = None) -> list[Listing]:
        names = self.active()
        if not names:
            return []

        async def one(name):
            a = self.adapters[name]
            if only is not None and getattr(a, "supports_only", False):
                return await a.get_listings(only=only)
            return await a.get_listings()

        results = await asyncio.gather(*(one(n) for n in names), return_exceptions=True)
        out, failures = [], []
        for name, res in zip(names, results):
            if isinstance(res, BaseException):
                if not isinstance(res, Exception):
                    raise res
                failures.append(res)
                if self.errors.get(name) != str(res):
                    log.warning("%s poll failed: %s", name, res)
                self.errors[name] = f"{type(res).__name__}: {res}"
            else:
                self.errors.pop(name, None)
                out.extend(res)
        if failures and len(failures) == len(names):
            raise failures[0]
        return out

    async def get_listing_details(self, listing_id: str) -> Listing | None:
        from .starpets import PREFIX
        name = "starpets" if listing_id.startswith(PREFIX) else "bloxswaps"
        a = self.adapters.get(name)
        return await a.get_listing_details(listing_id) if a else None

    def item_names(self) -> list[str]:
        names = set()
        for n in self.active():
            fn = getattr(self.adapters[n], "item_names", None)
            if fn:
                names.update(fn())
        return sorted(names)

    # ---- claiming: BloxSwaps only

    async def claim_listing(self, listing: Listing, idem_key: str) -> ClaimOutcome:
        if not self.can_claim(listing):
            return ClaimOutcome(ClaimStatus.SKIPPED,
                                detail=f"{listing.marketplace} cannot claim (alert only)")
        return await self.adapters[listing.marketplace].claim_listing(listing, idem_key)

    async def claim_state(self, claim_id, listing):
        return await self.primary.claim_state(claim_id, listing)

    async def reconcile_claim(self, listing):
        return await self.primary.reconcile_claim(listing)

    async def fetch_handoff(self, claim_id):
        return await self.primary.fetch_handoff(claim_id)

    async def get_balance(self):
        return await self.primary.get_balance()

    async def _read_account(self):
        return await self.primary._read_account()
