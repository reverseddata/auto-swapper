"""SQLite persistence: seen listings, claims, and the polling hold.

One file, WAL mode, no server. Calls are short and synchronous, and a lock
serialises them.

Safety-critical parts:
* `reserve_claim` checks the daily cap and inserts a PENDING row in one
  IMMEDIATE transaction before any request is sent. The idempotency key is
  UNIQUE, so two concurrent attempts at the same item cannot both proceed.
* The hold is stored in the database, so "stop polling until claimed"
  survives a restart. A crash right after a claim can't resume sniping and
  claim again.
"""
from __future__ import annotations

import enum
import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .models import OPEN_STATUSES, ClaimStatus, Listing

log = logging.getLogger(__name__)

# Statuses whose balance counts as spent (or possibly spent). A timed-out claim
# may still be traded, so it keeps counting toward the daily cap.
COMMITTED = tuple(s.value for s in (ClaimStatus.PENDING, ClaimStatus.UNKNOWN,
                                    ClaimStatus.CLAIMED, ClaimStatus.COMPLETED,
                                    ClaimStatus.TIMED_OUT))

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    marketplace TEXT NOT NULL,
    listing_id  TEXT NOT NULL,
    name        TEXT,
    price       TEXT NOT NULL,
    available   INTEGER NOT NULL,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    PRIMARY KEY (marketplace, listing_id)
);
CREATE TABLE IF NOT EXISTS claims (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    idem_key    TEXT NOT NULL UNIQUE,
    marketplace TEXT NOT NULL,
    listing_id  TEXT NOT NULL,
    side        TEXT NOT NULL,
    name        TEXT NOT NULL,
    price       TEXT NOT NULL,
    unit        TEXT NOT NULL,
    day         TEXT NOT NULL,
    status      TEXT NOT NULL,
    claim_id    TEXT,
    detail      TEXT,
    handoff     TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS claims_day ON claims(day);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watchlist (
    key        TEXT PRIMARY KEY,
    item       TEXT NOT NULL,
    side       TEXT NOT NULL,
    max_price  TEXT,
    min_price  TEXT,
    updated_at TEXT NOT NULL
);
"""


class StoreError(RuntimeError):
    pass


class Change(str, enum.Enum):
    NEW = "new"                  # first sighting, in stock
    PRICE_CHANGED = "price_changed"
    RESTOCKED = "restocked"      # was out of stock, now in stock
    UNCHANGED = "unchanged"
    UNAVAILABLE = "unavailable"  # out of stock

    @property
    def needs_evaluation(self) -> bool:
        return self in (Change.NEW, Change.PRICE_CHANGED, Change.RESTOCKED)


@dataclass(frozen=True)
class Reservation:
    ok: bool
    reason: str = ""
    claim_row_id: int | None = None
    quantity: int = 1          # units granted (the daily cap may grant fewer)


@dataclass(frozen=True)
class Hold:
    """Polling is paused until this claim is traded (or the operator resumes)."""
    idem_key: str
    name: str
    since: datetime
    until: datetime | None   # auto-resume deadline; None = manual resume only


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_day(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%d")


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        # FULL: a PENDING row must survive power loss, or a restart could claim again.
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(SCHEMA)
        # Fallback dedupe if the database becomes unwritable mid-run.
        self._mem_seen: dict[tuple[str, str], tuple[str, bool]] = {}

    def close(self):
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, params=()) -> sqlite3.Cursor:
        try:
            return self._conn.execute(sql, params)
        except sqlite3.Error as e:
            raise StoreError(f"{type(e).__name__}: {e}") from e

    # ------------------------------------------------------------ dedupe

    def observe(self, listing: Listing) -> Change:
        """Record a sighting and report what changed since the last one."""
        key = (listing.marketplace, listing.listing_id)
        price, avail, now = str(listing.price), bool(listing.available), _now()
        try:
            with self._lock:
                row = self._exec("SELECT price, available FROM seen WHERE marketplace=? "
                                 "AND listing_id=?", key).fetchone()
                self._exec(
                    "INSERT INTO seen(marketplace, listing_id, name, price, available,"
                    " first_seen, last_seen) VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(marketplace, listing_id) DO UPDATE SET name=excluded.name,"
                    " price=excluded.price, available=excluded.available,"
                    " last_seen=excluded.last_seen",
                    (*key, listing.name, price, int(avail), now, now))
            prev = None if row is None else (row["price"], bool(row["available"]))
        except StoreError as e:
            log.warning("seen-store unavailable, using in-memory dedupe: %s", e)
            prev = self._mem_seen.get(key)
        self._mem_seen[key] = (price, avail)
        if not avail:
            return Change.UNAVAILABLE
        if prev is None:
            return Change.NEW
        if not prev[1]:
            return Change.RESTOCKED
        if Decimal(prev[0]) != listing.price:
            return Change.PRICE_CHANGED
        return Change.UNCHANGED

    def mark_unavailable(self, marketplace: str, listing_id: str):
        """Record the listing as out of stock, so if it is still there on the
        next poll it reads as a restock and is tried again (used when a claim
        was skipped for a transient reason)."""
        key = (marketplace, listing_id)
        if key in self._mem_seen:
            self._mem_seen[key] = (self._mem_seen[key][0], False)
        try:
            with self._lock:
                self._exec("UPDATE seen SET available=0 WHERE marketplace=? AND listing_id=?",
                           key)
        except StoreError as e:
            log.warning("could not reset %s: %s", listing_id, e)

    # ------------------------------------------------------------ claims

    def spent(self, day: str | None = None) -> Decimal:
        ph = ",".join("?" * len(COMMITTED))
        # Only withdrawals spend balance; deposits are paid to you.
        q = f"SELECT price FROM claims WHERE side='withdraw' AND status IN ({ph})"
        params = list(COMMITTED)
        if day:
            q += " AND day=?"
            params.append(day)
        with self._lock:
            rows = self._exec(q, params).fetchall()
        return sum((Decimal(r["price"]) for r in rows), Decimal(0))

    def reserve_claim(self, *, idem_key: str, listing: Listing,
                      daily_limit: Decimal | None, day: str | None = None,
                      quantity: int = 1) -> Reservation:
        """Atomically check the daily cap and insert a PENDING row for up to
        `quantity` units (fewer if only fewer fit the cap); the row's price is
        the total. Returns ok=False with a reason on duplicate, limit, or a
        broken ledger."""
        quantity = max(1, quantity)
        day = day or utc_day()
        now = _now()
        with self._lock:
            try:
                self._exec("BEGIN IMMEDIATE")
            except StoreError as e:
                return Reservation(False, f"ledger unavailable: {e}")
            try:
                dup = self._exec("SELECT status FROM claims WHERE idem_key=?",
                                 (idem_key,)).fetchone()
                if dup:
                    self._exec("ROLLBACK")
                    return Reservation(False, f"duplicate attempt (already {dup['status']})")
                ph = ",".join("?" * len(OPEN_STATUSES))
                active = self._exec(
                    f"SELECT name, status FROM claims WHERE status IN ({ph})",
                    tuple(s.value for s in OPEN_STATUSES)).fetchone()
                if active:
                    self._exec("ROLLBACK")
                    return Reservation(False, f"claim for {active['name']} is still "
                                              f"{active['status']}")
                if daily_limit is not None and listing.side == "withdraw":
                    ph = ",".join("?" * len(COMMITTED))
                    rows = self._exec(f"SELECT price FROM claims WHERE day=? AND "
                                      f"side='withdraw' AND status IN "
                                      f"({ph})", (day, *COMMITTED)).fetchall()
                    today = sum((Decimal(r["price"]) for r in rows), Decimal(0))
                    if listing.price > 0:
                        fits = int((daily_limit - today) // listing.price)
                        quantity = max(1, min(quantity, fits))
                    if today + listing.price * quantity > daily_limit:
                        self._exec("ROLLBACK")
                        return Reservation(False, f"daily cap: {today} + {listing.price} > "
                                                  f"{daily_limit}")
                cur = self._exec(
                    "INSERT INTO claims(idem_key, marketplace, listing_id, side, name, price,"
                    " unit, day, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (idem_key, listing.marketplace, listing.listing_id, listing.side,
                     listing.name,
                     str(listing.price * quantity), listing.unit, day,
                     ClaimStatus.PENDING.value, now, now))
                self._exec("COMMIT")
                return Reservation(True, claim_row_id=cur.lastrowid, quantity=quantity)
            except BaseException as e:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                if isinstance(e, StoreError):
                    return Reservation(False, f"ledger unavailable: {e}")
                raise

    def update_claim(self, idem_key: str, status: ClaimStatus, *, claim_id: str | None = None,
                     detail: str = "", handoff: dict | None = None):
        with self._lock:
            self._exec(
                "UPDATE claims SET status=?, claim_id=COALESCE(?, claim_id), detail=?,"
                " handoff=COALESCE(?, handoff), updated_at=? WHERE idem_key=?",
                (status.value, claim_id, detail[:500],
                 json.dumps(handoff) if handoff else None, _now(), idem_key))

    def claim(self, idem_key: str) -> sqlite3.Row | None:
        with self._lock:
            return self._exec("SELECT * FROM claims WHERE idem_key=?", (idem_key,)).fetchone()

    def open_claims(self) -> list[sqlite3.Row]:
        ph = ",".join("?" * len(OPEN_STATUSES))
        with self._lock:
            return self._exec(f"SELECT * FROM claims WHERE status IN ({ph}) ORDER BY id",
                              tuple(s.value for s in OPEN_STATUSES)).fetchall()

    def recent_claims(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            return self._exec("SELECT * FROM claims ORDER BY id DESC LIMIT ?",
                              (limit,)).fetchall()

    # ------------------------------------------------------------ watchlist

    def watch_entries(self) -> list:
        from .config import WatchEntry
        with self._lock:
            rows = self._exec("SELECT * FROM watchlist ORDER BY item").fetchall()
        return [WatchEntry(r["item"], r["side"],
                           Decimal(r["max_price"]) if r["max_price"] else None,
                           Decimal(r["min_price"]) if r["min_price"] else None, r["key"])
                for r in rows]

    def put_watch(self, w) -> None:
        with self._lock:
            self._exec(
                "INSERT INTO watchlist(key, item, side, max_price, min_price, updated_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET item=excluded.item, "
                "side=excluded.side, max_price=excluded.max_price, "
                "min_price=excluded.min_price, updated_at=excluded.updated_at",
                (w.key, w.item, w.side,
                 str(w.max_price) if w.max_price is not None else None,
                 str(w.min_price) if w.min_price is not None else None, _now()))

    def remove_watch(self, key: str) -> bool:
        with self._lock:
            return self._exec("DELETE FROM watchlist WHERE key=?", (key,)).rowcount > 0

    # ------------------------------------------------------------ kv

    def get_kv(self, key: str) -> str | None:
        with self._lock:
            row = self._exec("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return None if row is None else row["value"]

    def set_kv(self, key: str, value: str) -> None:
        with self._lock:
            self._exec("INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) "
                       "DO UPDATE SET value=excluded.value", (key, value))

    def delete_kv(self, key: str) -> None:
        with self._lock:
            self._exec("DELETE FROM kv WHERE key=?", (key,))

    # ------------------------------------------------------------ hold

    def set_hold(self, hold: Hold):
        data = {"idem_key": hold.idem_key, "name": hold.name, "since": hold.since.isoformat(),
                "until": hold.until.isoformat() if hold.until else None}
        with self._lock:
            self._exec("INSERT INTO kv(key, value) VALUES ('hold', ?) ON CONFLICT(key) "
                       "DO UPDATE SET value=excluded.value", (json.dumps(data),))

    def get_hold(self) -> Hold | None:
        with self._lock:
            row = self._exec("SELECT value FROM kv WHERE key='hold'").fetchone()
        if row is None:
            return None
        d = json.loads(row["value"])
        return Hold(d["idem_key"], d["name"], datetime.fromisoformat(d["since"]),
                    datetime.fromisoformat(d["until"]) if d.get("until") else None)

    def clear_hold(self):
        with self._lock:
            self._exec("DELETE FROM kv WHERE key='hold'")
