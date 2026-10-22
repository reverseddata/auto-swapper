"""StarPets monitoring, multi-market polling and /market. No network."""
from decimal import Decimal

import pytest

from conftest import FakeAdapter, FakeNotifier, listings, make_settings, run
from mm2sniper.config import ConfigError, parse_markets
from mm2sniper.http import HttpResult, TransientError
from mm2sniper.marketplaces.multi import MultiMarketAdapter
from mm2sniper.marketplaces.starpets import StarPetsAdapter, merge_rows
from mm2sniper.models import ClaimStatus, Listing
from mm2sniper.service import Watcher
from mm2sniper.store import Store

D = Decimal


def wrow(gid, name, stock, price, chroma=False, can=True):
    return {"good_id": gid, "display_name": name, "stocks_available": stock,
            "withdrawal_price": price, "can_withdrawal": can,
            "features": [{"key": "chroma", "value": "true" if chroma else "false"},
                         {"key": "rarity", "value": "godly"}]}


def drow(gid, name, slots, price):
    return {"good_id": gid, "display_name": name, "max_deposit": slots, "deposit_price": price}


class PagedHttp:
    """Serves catalog pages by side; records every request."""

    def __init__(self, pages):
        self.pages = pages            # {"withdrawal": [[rows], [rows]], "deposit": [...]}
        self.calls = []

    async def request_json(self, method, url, *, json_body=None, headers=None, timeout=None,
                           etag=None):
        side = url.rsplit("/", 1)[1]
        self.calls.append((side, json_body["page"]))
        if isinstance(self.pages, Exception):
            raise self.pages
        pages = self.pages[side]
        return HttpResult(200, {"data": {"items": pages[json_body["page"] - 1],
                                         "pages": len(pages)}})


# ---------------------------------------------------------------- parsing


def test_rows_become_listings_with_chroma_and_both_sides():
    ls = merge_rows({"g1": wrow("g1", "Seer", 2, "9000", chroma=True),
                     "g2": wrow("g2", "Harvester", 0, "330")},
                    {"g1": drow("g1", "Seer", 0, "8000"), "g2": drow("g2", "Harvester", 3, "300")})
    by = {l.listing_id: l for l in ls}
    seer = by["starpets/g1:withdraw"]
    assert seer.name == "Chroma Seer" and seer.chroma and seer.available and seer.price == D(9000)
    assert seer.marketplace == "starpets" and seer.unit == "StarPets value"
    assert not by["starpets/g1:deposit"].available
    assert not by["starpets/g2:withdraw"].available and by["starpets/g2:deposit"].quantity == 3


def test_only_watched_names_are_kept():
    ls = merge_rows({"g1": wrow("g1", "Seer", 1, "50"), "g2": wrow("g2", "Harvester", 1, "330")},
                    {}, only={"harvester"})
    assert [l.name for l in ls] == ["Harvester"]


def test_malformed_rows_are_skipped():
    ls = merge_rows({"g1": {"good_id": "g1"}, "g2": wrow("g2", "Harvester", "x", None)}, {})
    assert ls == []


def test_all_pages_are_read_and_sweeps_are_throttled():
    t = [0.0]
    http = PagedHttp({"withdrawal": [[wrow("a", "A", 1, "1")], [wrow("b", "B", 1, "1")]],
                      "deposit": [[drow("a", "A", 1, "1")]]})
    a = StarPetsAdapter(http, min_interval=20, clock=lambda: t[0])
    assert {l.name for l in run(a.get_listings())} == {"A", "B"}
    assert http.calls == [("withdrawal", 1), ("withdrawal", 2), ("deposit", 1)]
    t[0] = 5.0
    run(a.get_listings())
    assert len(http.calls) == 3                 # within 20s: cached, no requests
    t[0] = 25.0
    run(a.get_listings())
    assert len(http.calls) == 6


def test_starpets_never_claims():
    a = StarPetsAdapter(PagedHttp({"withdrawal": [[]], "deposit": [[]]}))
    assert a.capabilities.claim is False
    out = run(a.claim_listing(merge_rows({"g": wrow("g", "A", 1, "1")}, {})[0], "k"))
    assert out.status is ClaimStatus.SKIPPED


# ---------------------------------------------------------------- multi-market


def sp_listing(name="Harvester", price="300"):
    return merge_rows({"g9": wrow("g9", name, 1, price)}, {})[0]


class FakeStar(FakeAdapter):
    name = "starpets"

    def __init__(self, ls=None, exc=None):
        super().__init__(ls or [], claim=False)
        self.exc = exc

    async def get_listings(self, only=None):
        if self.exc:
            raise self.exc
        return list(self.ls)


def multi(blox, star, enabled=("bloxswaps", "starpets")):
    return MultiMarketAdapter({"bloxswaps": blox, "starpets": star}, lambda: set(enabled))


def test_listings_from_both_markets_are_merged():
    b = FakeAdapter(listings("bloxswaps_after.json"))
    m = multi(b, FakeStar([sp_listing()]))
    got = run(m.get_listings())
    assert {l.marketplace for l in got} == {"bloxswaps", "starpets"}


def test_one_market_down_keeps_the_other_running():
    b = FakeAdapter(listings("bloxswaps_after.json"))
    m = multi(b, FakeStar(exc=TransientError("starpets down")))
    got = run(m.get_listings())
    assert got and all(l.marketplace == "bloxswaps" for l in got)
    assert "starpets down" in m.errors["starpets"]


def test_all_markets_down_raises():
    b = FakeAdapter([])
    b.poll_exc = [TransientError("blox down")]
    m = multi(b, FakeStar(exc=TransientError("star down")))
    with pytest.raises(TransientError):
        run(m.get_listings())


def test_disabled_market_is_not_polled():
    star = FakeStar(exc=AssertionError("must not be polled"))
    m = multi(FakeAdapter(listings("bloxswaps_after.json")), star, enabled=("bloxswaps",))
    assert run(m.get_listings())


def test_claims_only_go_to_bloxswaps():
    b = FakeAdapter(listings("bloxswaps_after.json"))
    m = multi(b, FakeStar())
    assert not m.can_claim(sp_listing())
    out = run(m.claim_listing(sp_listing(), "k"))
    assert out.status is ClaimStatus.SKIPPED and not b.claim_calls


def test_starpets_match_is_alerted_not_claimed_in_claim_mode(tmp_path):
    s = make_settings(tmp_path, "claim", extra_env={"WATCH": "Harvester<=400"})
    b = FakeAdapter([])
    m = multi(b, FakeStar())
    n = FakeNotifier()
    w = Watcher(s, Store(":memory:"), m, n)
    out_of_stock = Listing(**{**sp_listing().__dict__, "available": False, "quantity": 0})
    run(w.process([out_of_stock]))
    acted = run(w.process([sp_listing()]))       # restocked on StarPets
    assert [a for _, a in acted] == ["notified"]
    assert n.kinds() == ["available"] and not b.claim_calls


# ---------------------------------------------------------------- settings + /market


def test_markets_setting():
    assert parse_markets("") == {"bloxswaps"}
    assert parse_markets("both") == {"bloxswaps", "starpets"}
    assert parse_markets("starpets") == {"starpets"}
    assert parse_markets("bloxswaps,starpets") == {"bloxswaps", "starpets"}
    assert parse_markets("ebay") is None


def test_bad_markets_value_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError):
        make_settings(tmp_path, "monitor", extra_env={"MARKETS": "ebay"})


def test_market_command_switches_without_restart(tmp_path):
    from mm2sniper.discord_bot import Commands
    from mm2sniper.watchlist import WatchList
    s = make_settings(tmp_path, "monitor", extra_env={
        "DISCORD_BOT_TOKEN": "bot-token-not-real-123", "DISCORD_MENTION_USER_ID": "1"})
    store = Store(":memory:")
    wl = WatchList(s, store)
    star = FakeStar(exc=TransientError("star down"))
    m = multi(FakeAdapter(listings("bloxswaps_after.json")), star, enabled=())
    m.enabled = wl.markets
    c = Commands(s, store, m, wl)
    assert wl.markets() == {"bloxswaps"}
    assert "Bloxswaps and Starpets" in c.market("both")
    assert wl.markets() == {"bloxswaps", "starpets"}
    run(m.get_listings())                       # polls both now
    assert "Starpets last poll failed" in c.market()
    assert "Choose" in c.market("ebay")
    assert WatchList(s, store).markets() == {"bloxswaps", "starpets"}   # survives restart
