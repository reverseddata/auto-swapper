"""Typed records shared by the adapter, the watcher and the claim engine."""
from __future__ import annotations

import enum
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?|-?\.\d+")


def parse_price(raw) -> Decimal | None:
    """Parse a marketplace price into a non-negative Decimal, or None.

    Accepts numbers and strings like "1.2", "1,200", " 7 ". Missing, empty,
    non-numeric, negative, NaN or infinite input gives None. A malformed price is
    "unknown", never zero, so it can't look like a free item.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, float) and (math.isnan(raw) or math.isinf(raw)):
        return None
    if isinstance(raw, (int, float, Decimal)):
        val = Decimal(str(raw))
    elif isinstance(raw, str):
        m = _NUMBER.search(raw.strip())
        if not m:
            return None
        try:
            val = Decimal(m.group().replace(",", ""))
        except InvalidOperation:
            return None
    else:
        return None
    if not val.is_finite() or val < 0:
        return None
    return val


def norm_name(s: str) -> str:
    """Matching key: lowercase, alphanumerics, single spaces, possessives dropped,
    and the "C." chroma shorthand spelled out.
    "C. Seer" / "chroma-seer" -> "chroma seer"; "Traveler's Axe" -> "traveler axe"."""
    t = re.sub(r"'s\b", "", (s or "").lower())
    t = re.sub(r"[^a-z0-9]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if t.startswith("c "):
        t = "chroma " + t[2:]
    return t


@dataclass(frozen=True)
class Listing:
    """One item the marketplace can hand over right now (or could, if restocked)."""
    marketplace: str
    listing_id: str            # stable per item+side (BloxSwaps: "<slug>:withdraw")
    slug: str                  # the item's own id on the marketplace
    side: str                  # "withdraw" (take from bot stock) or "deposit" (give into a slot)
    name: str                  # display name, with "Chroma " prepended for chromas
    # Marketplace's own unit (BloxSwaps: site value). withdraw: what you pay;
    # deposit: what they credit you.
    price: Decimal
    unit: str                  # e.g. "BloxSwaps value"
    available: bool            # withdraw: bot has stock; deposit: a slot is free
    quantity: int = 0          # units in stock / free slots
    rarity: str | None = None
    chroma: bool = False
    item_type: str | None = None
    url: str = ""
    image: str = ""
    detected_at: datetime = field(default_factory=utcnow)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Capabilities:
    monitoring: bool = True
    claim: bool = False
    claim_unavailable_reason: str = ""
    # Can the adapter ask the site whether an open claim finished?
    claim_status: bool = False


@dataclass
class MarketplaceStatus:
    name: str
    capabilities: Capabilities
    reachable: bool = False
    last_success: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    listings_tracked: int = 0
    polls: int = 0
    rate_limited_until: datetime | None = None
    paused_reason: str | None = None

    def as_dict(self) -> dict:
        iso = lambda d: d.isoformat() if d else None  # noqa: E731
        return {
            "name": self.name,
            "reachable": self.reachable,
            "last_success": iso(self.last_success),
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "listings_tracked": self.listings_tracked,
            "polls": self.polls,
            "rate_limited_until": iso(self.rate_limited_until),
            "paused_reason": self.paused_reason,
            "supports_claim": self.capabilities.claim,
            "claim_unavailable_reason": self.capabilities.claim_unavailable_reason,
        }


class ClaimStatus(str, enum.Enum):
    PENDING = "pending"          # reserved locally, request not answered yet
    CLAIMED = "claimed"          # site confirmed; a human must now do the trade
    COMPLETED = "completed"      # trade done (site said so, or operator resumed)
    EXPIRED = "expired"          # site says the claim lapsed / was cancelled
    REJECTED = "rejected"        # site refused (gone, price moved, balance...)
    TIMED_OUT = "timed_out"      # hold ran out with no final answer: may still be live
    UNKNOWN = "unknown"          # outcome unclear: reconcile, never retry blindly
    SKIPPED = "skipped"          # failed revalidation or limits before any request


OPEN_STATUSES = (ClaimStatus.PENDING, ClaimStatus.UNKNOWN, ClaimStatus.CLAIMED)


@dataclass(frozen=True)
class ClaimOutcome:
    status: ClaimStatus
    claim_id: str | None = None
    detail: str = ""
    # True only when the request provably never reached the marketplace.
    safe_to_retry: bool = False
    # What the human needs to finish the trade.
    server_link: str = ""
    username: str = ""
    bot_name: str = ""
    # What the site put on the trade for our side (withdraw: cost; deposit: credit).
    charged: Decimal | None = None
