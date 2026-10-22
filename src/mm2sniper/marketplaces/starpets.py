"""StarPets Swap adapter (MONITORING ONLY).

StarPets Swap (starpets.gg/swap) is a bot-run swap desk like BloxSwaps, separate
from their user marketplace. Its item list is public (no login):

    POST https://swaps.apineural.com/api/v2/goods/catalog/withdrawal
    POST https://swaps.apineural.com/api/v2/goods/catalog/deposit
    body {"game": 4, "sorting": {"field": "name", "direction": "asc"},
          "limit": 72, "page": N}             game 4 = MM2, limit 10..72

    withdrawal rows: good_id, display_name, stocks_available, withdrawal_price,
                     can_withdrawal, features[], image_url
    deposit rows:    good_id, max_deposit, deposit_price

Recorded 2026-08-28 in the earlier sniper; StarPets was down while this adapter was
written (2026-10-22), so these calls are not re-verified. Prices are MM2 value units.
Chromas are named plainly; the flag lives in `features` (key "chroma").

Claiming is OFF: nobody has seen StarPets' swap/claim requests or how its login
works, and guessing them against a real account is not acceptable. Matches on
StarPets are sent as Discord alerts. To add claiming, capture the real calls once
(a HAR from a working swap) and implement claim_listing here.

A full sweep is up to ~28 requests, so it runs at most every MIN_INTERVAL seconds
however fast the main loop polls; in between the last result is reused.
"""
from __future__ import annotations

import logging
import time

from ..http import BadResponse, HttpClient
from ..models import Capabilities, Listing, norm_name, parse_price
from .base import MarketplaceAdapter

log = logging.getLogger(__name__)

API = "https://swaps.apineural.com/api/v2/goods/catalog"
SWAP_URL = "https://starpets.gg/swap"
UNIT = "StarPets value"
GAME_MM2 = 4
PAGE_SIZE = 72
MAX_PAGES = 25
MIN_INTERVAL = 20.0
HEADERS = {"Origin": "https://starpets.gg", "Referer": "https://starpets.gg/"}
PREFIX = "starpets/"


def features(row: dict) -> dict:
    """`features` is a list of {key, title, value}; flatten it."""
    out = {}
    for f in row.get("features") or []:
        if isinstance(f, dict) and f.get("key"):
            out[f["key"]] = f.get("value")
    return out


def _int(v) -> int:
    try:
        return max(int(float(v or 0)), 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def merge_rows(withdrawal: dict, deposit: dict, only: set[str] | None = None) -> list[Listing]:
    """Both catalog sides (keyed by good_id) -> listings. With `only`, rows whose
    name is not watched are skipped before any further work."""
    out: list[Listing] = []
    for gid in sorted(set(withdrawal) | set(deposit)):
        w, d = withdrawal.get(gid) or {}, deposit.get(gid) or {}
        f = features(w) or features(d)
        raw = str(w.get("display_name") or d.get("display_name") or "").strip()
        if not raw:
            continue
        chroma = str(f.get("chroma")).lower() == "true"
        name = f"Chroma {raw}" if chroma and not raw.lower().startswith("chroma") else raw
        if only is not None and norm_name(name) not in only and norm_name(gid) not in only:
            continue
        common = dict(marketplace="starpets", slug=gid, name=name, unit=UNIT,
                      rarity=f.get("rarity") or None, chroma=chroma,
                      item_type=f.get("category"), url=SWAP_URL,
                      image=str(w.get("image_url") or d.get("image_url") or ""),
                      extra={"id": gid})
        sell = parse_price(w.get("withdrawal_price"))
        if w and sell is not None:
            stock = _int(w.get("stocks_available"))
            can = w.get("can_withdrawal", True) is not False
            out.append(Listing(listing_id=f"{PREFIX}{gid}:withdraw", side="withdraw",
                               price=sell, available=can and stock > 0, quantity=stock,
                               **common))
        buy = parse_price(d.get("deposit_price"))
        if d and buy is not None:
            slots = _int(d.get("max_deposit"))
            out.append(Listing(listing_id=f"{PREFIX}{gid}:deposit", side="deposit",
                               price=buy, available=slots > 0, quantity=slots, **common))
    return out


class StarPetsAdapter(MarketplaceAdapter):
    name = "starpets"
    capabilities = Capabilities(
        monitoring=True, claim=False,
        claim_unavailable_reason="StarPets is monitor-only for now (alerts, no claims)")
    supports_only = True

    def __init__(self, http: HttpClient, *, timeout: float = 20.0,
                 min_interval: float = MIN_INTERVAL, clock=time.monotonic):
        super().__init__()
        self.http = http
        self.timeout = timeout
        self.min_interval = min_interval
        self._clock = clock
        self._fetched_at: float | None = None
        self._withdrawal: dict = {}
        self._deposit: dict = {}

    async def _sweep(self, side: str) -> dict:
        rows, page = {}, 1
        while page <= MAX_PAGES:
            body = {"game": GAME_MM2, "sorting": {"field": "name", "direction": "asc"},
                    "limit": PAGE_SIZE, "page": page}
            res = await self.http.request_json("POST", f"{API}/{side}", json_body=body,
                                               headers=HEADERS, timeout=self.timeout)
            data = res.data.get("data") if isinstance(res.data, dict) else None
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise BadResponse(f"unexpected StarPets {side} payload")
            for row in data["items"]:
                if isinstance(row, dict) and row.get("good_id"):
                    rows[str(row["good_id"])] = row
            if page >= _int(data.get("pages") or 1):
                break
            page += 1
        return rows

    async def _refresh(self, force: bool = False):
        now = self._clock()
        if (not force and self._fetched_at is not None
                and now - self._fetched_at < self.min_interval):
            return
        wd = await self._sweep("withdrawal")
        dp = await self._sweep("deposit")
        if not wd and not dp:
            raise BadResponse("StarPets returned no items")
        self._withdrawal, self._deposit, self._fetched_at = wd, dp, now

    async def get_listings(self, only: set[str] | None = None) -> list[Listing]:
        await self._refresh()
        return merge_rows(self._withdrawal, self._deposit, only)

    async def get_listing_details(self, listing_id: str) -> Listing | None:
        await self._refresh(force=True)
        return next((l for l in merge_rows(self._withdrawal, self._deposit)
                     if l.listing_id == listing_id), None)

    def item_names(self) -> list[str]:
        return sorted({l.name for l in merge_rows(self._withdrawal, self._deposit)})
