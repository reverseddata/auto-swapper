"""BloxSwaps adapter.

Monitoring (verified 2026-10-17): `GET https://api.bloxswaps.com/items` is the
public, unauthenticated request the trade page itself makes. One request returns
every item (~958, ~460 KB, uncompressed) with:

    appAvailableQuantity   units the bots hold      -> WITHDRAW side (claim one)
    userAvailableQuantity  free deposit slots       -> DEPOSIT side (claim a slot)
    currentPrices.sellPrice  what a withdrawal costs (site value units, string)
    currentPrices.buyPrice   what a deposit credits  (site value units, string)
    chroma                   true for chromas; displayName does NOT say "Chroma"

Each item becomes two listings, "<slug>:withdraw" and "<slug>:deposit".

Claiming (read from the public site bundle, 2026-10-18; not yet checked against
a live account, so the first real claim is the test):

    GET    /auth/me                 data.user.robloxUsername: the account name
    POST   /trades                  {"items":[{"itemDetailsId", "quantity", "tradeRole"}]}
           tradeRole APP = bot gives the item (withdraw), USER = you give it (deposit)
           response data: trade {id, status, botTrades:[{status, joinUrl, bot:{name}, items}]}
    GET    /trades/{id}             data.status: IN_PROGRESS | PARTIALLY_COMPLETED | COMPLETED | CANCELLED
    GET    /trades/active           404 when no trade is open
    POST   /auth/refresh            new access cookie in Set-Cookie (the site calls it on a 401)

Auth is a logged-in session, sent as cookies (not a bearer token). BLOXSWAPS_TOKEN
holds the Cookie header value. When the access cookie expires (about 15 min), the
adapter calls /auth/refresh once and retries. The rotated cookies are saved to
DATA_DIR so a restart does not need a fresh paste.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

from ..http import (AuthError, BadResponse, HttpClient, MarketplaceError, RateLimited,
                    RequestNotSent, TransientError)
from ..models import Capabilities, ClaimOutcome, ClaimStatus, Listing, parse_price
from ..notify import register_secret
from .base import MarketplaceAdapter

log = logging.getLogger(__name__)

API_URL = "https://api.bloxswaps.com"
ITEMS_URL = f"{API_URL}/items"
TRADES_URL = f"{API_URL}/trades"
ME_URL = f"{API_URL}/auth/me"
BALANCE_URL = f"{API_URL}/account/balance"
REFRESH_URL = f"{API_URL}/auth/refresh"
TRADE_URL = "https://bloxswaps.com/trade-mm2"
UNIT = "BloxSwaps value"
HEADERS = {"Origin": "https://bloxswaps.com", "Referer": "https://bloxswaps.com/"}
ROLE = {"withdraw": "APP", "deposit": "USER"}
MM2_PLACE_ID = "142823291"
# A server id goes into a link people click, so only plain ids are accepted.
_SERVER_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_SAFE_JOIN_PREFIXES = ("https://www.roblox.com/", "roblox://")
_DELIVERY_ENDED = ("DONE", "CANCELLED", "RETURNING")
_TRADE_STATUS = {
    "IN_PROGRESS": ClaimStatus.CLAIMED,
    "COMPLETED": ClaimStatus.COMPLETED,
    "PARTIALLY_COMPLETED": ClaimStatus.COMPLETED,
    "CANCELLED": ClaimStatus.EXPIRED,
}


def item_url(slug: str) -> str:
    return f"{TRADE_URL}?item={quote(slug, safe='')}" if slug else TRADE_URL


def display_name(row: dict) -> str:
    name = str(row.get("displayName") or row.get("slug") or "").strip()
    if row.get("chroma") and not name.lower().startswith("chroma"):
        name = f"Chroma {name}"
    return name


def normalize_rows(payload) -> tuple[list[Listing], int]:
    """Payload -> (listings, malformed_row_count). Raises BadResponse if the
    payload as a whole has the wrong shape."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise BadResponse("unexpected /items payload shape")
    out: list[Listing] = []
    bad = 0
    seen: set[str] = set()
    for row in payload["data"]:
        try:
            slug = str(row["slug"]).strip()
            if not slug or slug in seen:
                bad += not slug
                continue  # duplicate slug: keep the first
            prices = row.get("currentPrices") or {}
            sell = parse_price(prices.get("sellPrice"))
            buy = parse_price(prices.get("buyPrice"))
            tradable = row.get("tradable", True) is not False
            app = int(row.get("appAvailableQuantity") or 0)
            user = int(row.get("userAvailableQuantity") or 0)
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            bad += 1  # OverflowError: int(float("inf")) from a JSON Infinity
            continue
        seen.add(slug)
        common = dict(marketplace="bloxswaps", slug=slug, name=display_name(row), unit=UNIT,
                      rarity=(row.get("rarity") or None), chroma=bool(row.get("chroma")),
                      item_type=row.get("type"), url=item_url(slug),
                      image=str(row.get("image") or ""),
                      extra={"id": row.get("id"), "stock_limit": row.get("stockLimit")})
        if sell is not None:
            out.append(Listing(listing_id=f"{slug}:withdraw", side="withdraw", price=sell,
                               available=tradable and app > 0, quantity=max(app, 0), **common))
        else:
            bad += 1
        if buy is not None:
            out.append(Listing(listing_id=f"{slug}:deposit", side="deposit", price=buy,
                               available=tradable and user > 0, quantity=max(user, 0), **common))
    return out, bad


def _trade_items(trade: dict):
    """Every (itemDetailsId, tradeRole) pair on a trade's bot legs."""
    for leg in trade.get("botTrades") or []:
        if not isinstance(leg, dict):
            continue
        for item in leg.get("items") or []:
            if isinstance(item, dict):
                yield item.get("itemDetailsId"), item.get("tradeRole")


def join_link(server_id: str) -> str:
    """https link that opens the MM2 server in Roblox. The site itself uses
    roblox://placeId=..&gameInstanceId=..; Discord only links http(s)."""
    return (f"https://www.roblox.com/games/start?placeId={MM2_PLACE_ID}"
            f"&gameInstanceId={server_id}")


def _clean_name(value) -> str:
    """Bot names come from the site and end up in Discord: printable, short."""
    return "".join(ch for ch in str(value or "") if ch.isprintable())[:64]


def handoff_from_trade(trade: dict) -> tuple[str, str]:
    """(join link, bot name) for the player. A new trade has `delivery`: the bot
    collecting it and the server it waits in. Older bot-leg responses are a fallback.
    Anything malformed gives ("", ""), so a bad hand-off never loses the trade id."""
    try:
        delivery = trade.get("delivery") or {}
        if delivery.get("status") in _DELIVERY_ENDED:
            return "", ""  # the site shows no join button for an ended delivery either
        bot = delivery.get("bot") or {}
        server_id = str((delivery.get("server") or {}).get("id") or "")
        if server_id:
            link = join_link(server_id) if _SERVER_ID_RE.match(server_id) else ""
            return link, _clean_name(bot.get("name"))
        for leg in trade.get("botTrades") or []:
            url = str(leg.get("joinUrl") or "")
            if (leg.get("status", "IN_PROGRESS") == "IN_PROGRESS" and url
                    and url.startswith(_SAFE_JOIN_PREFIXES)):
                return url, _clean_name((leg.get("bot") or {}).get("name"))
    except (AttributeError, TypeError):
        pass
    return "", ""


def _matches_open_trade(trade: dict, listing: Listing) -> bool:
    """Does this open trade hold our listing? Legs list items. A brand-new trade has
    no legs yet, so fall back to its value on our side of the trade."""
    wanted = (listing.extra.get("id"), ROLE[listing.side])
    try:
        if wanted in set(_trade_items(trade)):
            return True
    except (AttributeError, TypeError):
        return False
    if trade.get("botTrades"):
        return False
    field = "appItemsValue" if listing.side == "withdraw" else "userItemsValue"
    other = "userItemsValue" if listing.side == "withdraw" else "appItemsValue"
    try:
        return (Decimal(str(trade.get(field))) == listing.price
                and Decimal(str(trade.get(other) or "0")) == 0)
    except (InvalidOperation, TypeError):
        return False


def parse_cookie_header(header: str) -> dict[str, str]:
    """"a=1; b=2" -> {"a": "1", "b": "2"}. Values are kept exactly as copied."""
    jar: dict[str, str] = {}
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            jar[name] = value
    return jar


def cookie_header(jar: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in jar.items())


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class BloxSwapsAdapter(MarketplaceAdapter):
    name = "bloxswaps"
    # Verified 2026-10-18: the POST /trades answer has no bot or server yet; they
    # appear on GET /trades/{id} a moment later (delivery.bot, delivery.server).
    supports_handoff = True

    def __init__(self, http: HttpClient, *, token: str = "", timeout: float = 20.0,
                 secrets: dict[str, str] | None = None, session_path: Path | None = None):
        # `token` is the Cookie header value of a logged-in session (see README).
        self.token = token.strip()
        self.capabilities = Capabilities(
            monitoring=True, claim=bool(self.token), claim_status=bool(self.token),
            claim_unavailable_reason="" if self.token else
            "claiming is not configured (BLOXSWAPS_TOKEN not set)")
        super().__init__()
        self.http = http
        self.secrets = secrets or {}
        self.timeout = timeout
        self._etag: str | None = None
        self._cache: list[Listing] = []
        self.jar = parse_cookie_header(self.token)
        self._session_path = session_path
        self._refresh_lock = asyncio.Lock()
        self._refresh_task: asyncio.Future | None = None
        self._load_session()
        # Every cookie value is a secret: the redactor scrubs them exactly, in any form.
        register_secret(self.token)
        for value in self.jar.values():
            register_secret(value)

    # ------------------------------------------------------------ session

    def _load_session(self):
        """Use cookies rotated by an earlier run, but only if they belong to the
        token in .env now. A new paste in .env always wins."""
        if self._session_path is None or not self.token:
            return
        try:
            saved = json.loads(self._session_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(saved, dict) and saved.get("token_sha") == _fingerprint(self.token):
            cookies = saved.get("cookies")
            if isinstance(cookies, dict):
                self.jar.update({str(k): str(v) for k, v in cookies.items()})

    def _save_session(self):
        if self._session_path is None:
            return
        try:
            self._session_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._session_path.with_suffix(".tmp")
            # Created owner-only from the start (no window where it is world-readable).
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps({"token_sha": _fingerprint(self.token), "cookies": self.jar}))
            tmp.replace(self._session_path)
        except OSError as e:
            log.warning("could not save the BloxSwaps session: %s", e)

    async def _refresh(self, used: str):
        """Rotate the session cookies. `used` is the Cookie header that got the 401; if
        another request already rotated past it, there is nothing left to do."""
        async with self._refresh_lock:
            if cookie_header(self.jar) != used:
                return
            # One shared task: every caller that hit the 401 awaits the same refresh,
            # even if an earlier caller was cancelled while it ran.
            if self._refresh_task is None or self._refresh_task.done():
                self._refresh_task = asyncio.ensure_future(self._do_refresh())
                self._refresh_task.add_done_callback(
                    lambda t: t.cancelled() or t.exception())  # never "unretrieved"
            task = self._refresh_task
        # Shielded: once the site rotates the cookie, the new one must be kept even if
        # this caller is cancelled (e.g. the claim timeout), or the login is lost.
        await asyncio.shield(task)

    async def _do_refresh(self):
        res = await self.http.request_json("POST", REFRESH_URL, headers=self._cookie_headers(),
                                           timeout=self.timeout)
        if res.cookies:
            for value in res.cookies.values():
                register_secret(value)
            self.jar.update(res.cookies)
            self._save_session()
            log.info("BloxSwaps session refreshed")

    def _cookie_headers(self, header: str | None = None) -> dict[str, str]:
        return {**HEADERS, "Cookie": header if header is not None else cookie_header(self.jar)}

    async def _authed(self, method: str, url: str, json_body=None):
        """One request with the session. On a 401: refresh once, retry once. The site
        rejects an expired cookie before doing anything, so the retry cannot double-act."""
        used = cookie_header(self.jar)
        try:
            return await self.http.request_json(method, url, json_body=json_body,
                                                headers=self._cookie_headers(used),
                                                timeout=self.timeout)
        except AuthError:
            pass
        await self._refresh(used)
        return await self.http.request_json(method, url, json_body=json_body,
                                            headers=self._cookie_headers(),
                                            timeout=self.timeout)

    async def get_balance(self) -> Decimal:
        """Site balance in BloxSwaps value (GET /account/balance). Read-only."""
        res = await self._authed("GET", BALANCE_URL)
        data = res.data.get("data") if isinstance(res.data, dict) else None
        value = parse_price((data or {}).get("siteBalance") if isinstance(data, dict) else None)
        if value is None:
            raise BadResponse("balance answer had no siteBalance")
        return value

    async def _read_account(self) -> str:
        """Roblox username of the logged-in session (GET /auth/me). Read once, then cached."""
        if self.account:
            return self.account
        res = await self._authed("GET", ME_URL)
        data = res.data.get("data") if isinstance(res.data, dict) else None
        user = data.get("user") if isinstance(data, dict) else None
        name = str((user or {}).get("robloxUsername") or "").strip()
        if not name:
            raise BadResponse("session answered without a Roblox username")
        self.account = name
        return name

    # ------------------------------------------------------------ monitoring

    async def get_listings(self) -> list[Listing]:
        res = await self.http.request_json("GET", ITEMS_URL, headers=HEADERS,
                                           timeout=self.timeout, etag=self._etag)
        if res.not_modified:
            return self._cache
        listings, bad = await asyncio.to_thread(normalize_rows, res.data)
        if bad:
            log.debug("bloxswaps: skipped %d malformed rows", bad)
        if not listings:
            raise BadResponse("bloxswaps returned no usable items")
        self._etag, self._cache = res.etag, listings
        return listings

    # ------------------------------------------------------------ claiming

    async def claim_listing(self, listing: Listing, idem_key: str) -> ClaimOutcome:
        """POST /trades for one item on one side (withdraw: listing.quantity units). Withdraw takes from bot stock
        (bot gives it: APP leg). Deposit gives an item to the bot (USER leg).

        The site has no idempotency key, so the engine's local reservation is what
        stops a double claim. Errors the site answered with are REJECTED. Anything
        sent but not clearly answered is UNKNOWN, and the engine reconciles it."""
        item_id = listing.extra.get("id")
        if not self.token:
            return ClaimOutcome(ClaimStatus.SKIPPED,
                                detail=self.capabilities.claim_unavailable_reason)
        if item_id is None:
            return ClaimOutcome(ClaimStatus.REJECTED, detail="listing has no item id")
        try:
            await self._read_account()
        except RequestNotSent:
            raise  # engine retries: nothing left the machine
        except (AuthError, BadResponse) as e:
            # The session itself is refused or unusable: a real stop, not a blip.
            return ClaimOutcome(ClaimStatus.REJECTED, detail=f"could not read session: {e}")
        except MarketplaceError as e:
            # Timeout or rate limit before anything was sent: skip, the next poll retries.
            return ClaimOutcome(ClaimStatus.SKIPPED, detail=f"session unreachable, nothing sent: {e}")
        body = {"items": [{"itemDetailsId": item_id, "quantity": max(1, listing.quantity) if listing.side == "withdraw" else 1,
                           "tradeRole": ROLE[listing.side]}]}
        try:
            res = await self._authed("POST", TRADES_URL, json_body=body)
        except RequestNotSent:
            raise  # never left the machine: the engine may retry safely
        except (AuthError, RateLimited) as e:
            return ClaimOutcome(ClaimStatus.REJECTED, detail=f"site refused: {e}")
        except BadResponse as e:
            if e.status is None or e.status < 400:
                # Answered, but unreadable (or a non-error status): it may have acted.
                return ClaimOutcome(ClaimStatus.UNKNOWN, detail=f"unreadable answer: {e}")
            return ClaimOutcome(ClaimStatus.REJECTED, detail=f"site refused: {e}")
        except TransientError as e:
            return ClaimOutcome(ClaimStatus.UNKNOWN, detail=f"no clear answer: {e}")

        trade = res.data.get("data") if isinstance(res.data, dict) else None
        if not isinstance(trade, dict) or trade.get("id") in (None, ""):
            return ClaimOutcome(ClaimStatus.UNKNOWN, detail="claim answered without a trade id")
        join_url, bot_name = handoff_from_trade(trade)
        charged = parse_price(trade.get("appItemsValue" if listing.side == "withdraw"
                                        else "userItemsValue"))
        return ClaimOutcome(ClaimStatus.CLAIMED, claim_id=str(trade["id"]),
                            detail=f"trade {trade['id']} created",
                            server_link=join_url, bot_name=bot_name, username=self.account,
                            charged=charged)

    async def claim_state(self, claim_id: str | None, listing: Listing | None) -> ClaimStatus:
        """With a trade id: read that trade. Without one (reconcile after an unclear
        answer): look for an open trade holding this listing's item and side."""
        if not self.token:
            return ClaimStatus.UNKNOWN
        if claim_id:
            res = await self._authed("GET", f"{TRADES_URL}/{quote(str(claim_id), safe='')}")
            trade = res.data.get("data") if isinstance(res.data, dict) else None
            status = trade.get("status") if isinstance(trade, dict) else None
            return _TRADE_STATUS.get(status, ClaimStatus.UNKNOWN)
        if listing is None:
            return ClaimStatus.UNKNOWN
        state, _ = await self.reconcile_claim(listing)
        return state

    async def fetch_handoff(self, claim_id: str) -> tuple[str, str]:
        res = await self._authed("GET", f"{TRADES_URL}/{quote(str(claim_id), safe='')}")
        trade = res.data.get("data") if isinstance(res.data, dict) else None
        return handoff_from_trade(trade) if isinstance(trade, dict) else ("", "")

    async def reconcile_claim(self, listing: Listing) -> tuple[ClaimStatus, str | None]:
        """Look for an open trade holding this listing. Found: CLAIMED plus its id.
        Not found is NOT proof of no claim: after a timeout the site may still be
        creating it. So it stays UNKNOWN, and the engine holds instead of retrying."""
        if not self.token:
            return ClaimStatus.UNKNOWN, None
        try:
            res = await self._authed("GET", f"{TRADES_URL}/active")
        except BadResponse as e:
            if e.status == 404:
                return ClaimStatus.UNKNOWN, None
            raise
        trade = res.data.get("data") if isinstance(res.data, dict) else None
        if isinstance(trade, dict) and _matches_open_trade(trade, listing):
            return ClaimStatus.CLAIMED, (str(trade.get("id")) if trade.get("id") else None)
        return ClaimStatus.UNKNOWN, None
