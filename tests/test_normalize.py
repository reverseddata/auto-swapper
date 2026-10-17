from decimal import Decimal

import pytest

from conftest import by_id, listings, payload
from mm2sniper.http import BadResponse
from mm2sniper.marketplaces.bloxswaps import normalize_rows
from mm2sniper.models import norm_name, parse_price


def test_each_item_becomes_withdraw_and_deposit_listing():
    ls = listings("bloxswaps_before.json")
    h_w, h_d = by_id(ls, "harvester:withdraw"), by_id(ls, "harvester:deposit")
    assert (h_w.side, h_w.price, h_w.available, h_w.quantity) == ("withdraw", Decimal("312.9"),
                                                                   False, 0)
    assert (h_d.side, h_d.price, h_d.available, h_d.quantity) == ("deposit", Decimal("290.1"),
                                                                   True, 5)
    assert h_w.url == "https://bloxswaps.com/trade-mm2?item=harvester"


def test_chroma_flag_is_put_in_the_name():
    ls = listings("bloxswaps_before.json")
    assert by_id(ls, "chroma-seer:withdraw").name == "Chroma Seer"
    assert by_id(ls, "seer:withdraw").name == "Seer"


def test_malformed_and_duplicate_rows_are_skipped_not_fatal():
    ls, bad = normalize_rows(payload("bloxswaps_before.json"))
    slugs = {l.slug for l in ls}
    assert slugs == {"harvester", "chroma-seer", "seer", "icepiercer"}
    assert bad >= 2
    # The duplicate "seer" row (qty 9) must not override the first.
    assert by_id(ls, "seer:withdraw").quantity == 2


def test_bad_price_drops_only_that_side():
    ls, _ = normalize_rows(payload("bloxswaps_after.json"))
    ids = {l.listing_id for l in ls}
    assert "seer:withdraw" not in ids and "seer:deposit" in ids


def test_untradable_is_unavailable():
    p = payload("bloxswaps_before.json")
    p["data"][2]["tradable"] = False
    ls, _ = normalize_rows(p)
    assert not by_id(ls, "seer:withdraw").available


@pytest.mark.parametrize("bad", [None, {}, {"data": "x"}, []])
def test_wrong_payload_shape_raises(bad):
    with pytest.raises(BadResponse):
        normalize_rows(bad)


@pytest.mark.parametrize("raw,expected", [
    ("1.2", Decimal("1.2")), ("1,200", Decimal("1200")), (" 7 ", Decimal("7")),
    (3, Decimal("3")), (2.5, Decimal("2.5")), ("$3.50", Decimal("3.50")),
    (None, None), ("", None), ("abc", None), (-1, None), ("-2", None),
    (float("nan"), None), (float("inf"), None), (True, None), ([], None),
])
def test_parse_price(raw, expected):
    assert parse_price(raw) == expected


@pytest.mark.parametrize("a,b", [
    ("C. Seer", "Chroma Seer"), ("chroma-seer", "Chroma Seer"), ("c seer", "CHROMA SEER"),
    ("Traveler's Axe", "Traveler Axe"),
])
def test_name_matching_equivalents(a, b):
    assert norm_name(a) == norm_name(b)


def test_plain_and_chroma_do_not_match():
    assert norm_name("Seer") != norm_name("Chroma Seer")
