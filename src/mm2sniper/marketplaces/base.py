"""Adapter interface. An adapter knows one marketplace's URLs, payload shapes
and capabilities, and nothing about watchlists, Discord or limits."""
from __future__ import annotations

import abc

from ..models import Capabilities, ClaimOutcome, ClaimStatus, Listing, MarketplaceStatus


class MarketplaceAdapter(abc.ABC):
    name: str = ""
    capabilities: Capabilities = Capabilities()
    # Account name the adapter read from its own session ("" if not known yet).
    # Shown in claim alerts, so the user never has to configure it.
    account: str = ""
    # Can fetch_handoff() fill in join details after the claim answer?
    supports_handoff: bool = False

    def __init__(self):
        self.status = MarketplaceStatus(self.name, self.capabilities)

    async def connect(self) -> None:
        """Prepare sessions / verify credentials. Must not raise for a missing
        optional credential; reflect it in capabilities instead."""

    async def close(self) -> None:
        """Release network resources."""

    @abc.abstractmethod
    async def get_listings(self) -> list[Listing]:
        """One full sweep. Raises http.MarketplaceError subclasses on failure.
        Malformed rows are skipped, never fatal."""

    async def get_listing_details(self, listing_id: str) -> Listing | None:
        """Fresh state of one listing (for revalidation right before a claim).
        None if it no longer exists."""
        for l in await self.get_listings():
            if l.listing_id == listing_id:
                return l
        return None

    def get_marketplace_status(self) -> MarketplaceStatus:
        return self.status

    async def claim_listing(self, listing: Listing, idem_key: str) -> ClaimOutcome:
        """Claim one unit. Must return UNKNOWN (not raise, not REJECTED) when the
        request may have reached the site but no clear answer came back."""
        return ClaimOutcome(ClaimStatus.SKIPPED,
                            detail=self.capabilities.claim_unavailable_reason
                            or "claiming is not supported")

    async def fetch_handoff(self, claim_id: str) -> tuple[str, str]:
        """(join link, bot name) for a created claim, read fresh from the site.
        ("", "") while the site has not assigned them yet."""
        return "", ""

    async def reconcile_claim(self, listing: Listing) -> tuple[ClaimStatus, str | None]:
        """After an unclear answer: did our claim create something? Returns the state
        and, if the site shows the claim, its id. Anything the site does not clearly
        show must be UNKNOWN, never REJECTED: the request may still be processing."""
        return await self.claim_state(None, listing), None

    async def claim_state(self, claim_id: str | None, listing: Listing | None) -> ClaimStatus:
        """Ask the site where an open claim stands. CLAIMED = still waiting for
        the trade, COMPLETED/EXPIRED = finished, UNKNOWN = couldn't tell."""
        return ClaimStatus.UNKNOWN
