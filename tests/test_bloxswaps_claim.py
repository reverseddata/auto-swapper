import pytest

import json

from conftest import listings, run
from mm2sniper.http import AuthError, BadResponse, HttpResult, RequestNotSent, TransientError
from mm2sniper.marketplaces.bloxswaps import (REFRESH_URL, TRADES_URL, BloxSwapsAdapter,
                                              cookie_header, parse_cookie_header)
from mm2sniper.models import ClaimStatus

ME = HttpResult(200, {"success": True, "data": {"user": {"robloxUsername": "TestPlayer"},
                                                "accessTokenExpiresAt": "x"}})


class FakeHttp:
    """Records requests, answers from a script. An Exception in the script is raised."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def request_json(self, method, url, *, json_body=None, headers=None,
                           timeout=None, etag=None):
        self.calls.append({"method": method, "url": url, "json": json_body,
                           "headers": headers or {}})
        ans = self.answers.pop(0)
        if isinstance(ans, Exception):
            raise ans
        return ans

    def posts(self):
        return [c for c in self.calls if c["method"] == "POST"]


def trade_ok(join="https://www.roblox.com/share?code=x&type=Server"):
    return HttpResult(200, {"success": True, "data": {
        "id": "trade-42", "status": "IN_PROGRESS",
        "botTrades": [{"id": "leg-1", "status": "IN_PROGRESS", "joinUrl": join,
                       "bot": {"name": "boxswap123"},
                       "items": [{"itemDetailsId": 7, "tradeRole": "APP", "quantity": 1}]}],
    }})


TOKEN = "_ga=x; bloxswaps.at=abc"


def adapter(http, token=TOKEN):
    return BloxSwapsAdapter(http, token=token)


def listing(side):
    return next(l for l in listings("bloxswaps_before.json")
                if l.side == side and l.extra.get("id") is not None and l.available)


def test_capabilities_follow_token():
    assert adapter(FakeHttp(), token="").capabilities.claim is False
    caps = adapter(FakeHttp()).capabilities
    assert caps.claim and caps.claim_status and caps.claim_unavailable_reason == ""


def test_withdraw_claim_takes_bot_stock_as_app_leg():
    http = FakeHttp(ME, trade_ok())
    l = listing("withdraw")
    out = run(adapter(http).claim_listing(l, "k"))
    post = http.posts()[0]
    assert post["url"] == TRADES_URL
    assert post["json"] == {"items": [{"itemDetailsId": l.extra["id"], "quantity": 1,
                                       "tradeRole": "APP"}]}
    assert post["headers"]["Cookie"] == TOKEN
    assert out.status is ClaimStatus.CLAIMED and out.claim_id == "trade-42"
    assert out.server_link.startswith("https://www.roblox.com/share")
    assert out.bot_name == "boxswap123"
    assert out.username == "TestPlayer"


def test_deposit_claim_gives_item_as_user_leg():
    http = FakeHttp(ME, trade_ok())
    run(adapter(http).claim_listing(listing("deposit"), "k"))
    assert http.posts()[0]["json"]["items"][0]["tradeRole"] == "USER"


def test_account_is_read_from_session_once():
    http = FakeHttp(ME, trade_ok(), trade_ok())
    a = adapter(http)
    run(a.claim_listing(listing("withdraw"), "k1"))
    run(a.claim_listing(listing("withdraw"), "k2"))
    me_calls = [c for c in http.calls if c["url"].endswith("/auth/me")]
    assert len(me_calls) == 1 and a.account == "TestPlayer"


def test_unreadable_session_claims_nothing():
    http = FakeHttp(AuthError("401"), AuthError("401"))  # session read, then refresh: both dead
    out = run(adapter(http).claim_listing(listing("withdraw"), "k"))
    assert out.status is ClaimStatus.REJECTED and "session" in out.detail
    assert [c for c in http.posts() if c["url"] == TRADES_URL] == []


def test_session_without_username_claims_nothing():
    http = FakeHttp(HttpResult(200, {"data": {"user": {}}}))
    out = run(adapter(http).claim_listing(listing("withdraw"), "k"))
    assert out.status is ClaimStatus.REJECTED and http.posts() == []


def test_site_refusal_is_rejected_not_unknown():
    out = run(adapter(FakeHttp(ME, BadResponse("HTTP 400", 400))).claim_listing(
        listing("withdraw"), "k"))
    assert out.status is ClaimStatus.REJECTED
    out = run(adapter(FakeHttp(ME, AuthError("401"), AuthError("401"))).claim_listing(
        listing("withdraw"), "k"))
    assert out.status is ClaimStatus.REJECTED


def test_server_error_is_unknown_and_connect_failure_propagates():
    out = run(adapter(FakeHttp(ME, TransientError("HTTP 502"))).claim_listing(
        listing("withdraw"), "k"))
    assert out.status is ClaimStatus.UNKNOWN
    with pytest.raises(RequestNotSent):
        run(adapter(FakeHttp(ME, RequestNotSent("dns"))).claim_listing(listing("withdraw"), "k"))


def test_answer_without_trade_id_is_unknown():
    http = FakeHttp(ME, HttpResult(200, {"success": True, "data": {}}))
    out = run(adapter(http).claim_listing(listing("withdraw"), "k"))
    assert out.status is ClaimStatus.UNKNOWN


def test_claim_state_by_trade_id():
    cases = {"IN_PROGRESS": ClaimStatus.CLAIMED, "COMPLETED": ClaimStatus.COMPLETED,
             "PARTIALLY_COMPLETED": ClaimStatus.COMPLETED, "CANCELLED": ClaimStatus.EXPIRED}
    for site, want in cases.items():
        http = FakeHttp(HttpResult(200, {"data": {"id": "trade-42", "status": site}}))
        assert run(adapter(http).claim_state("trade-42", None)) is want
        assert http.calls[0]["url"] == f"{TRADES_URL}/trade-42"


def test_reconcile_without_trade_id_matches_item_and_side():
    l = listing("withdraw")
    other = {"itemDetailsId": 999, "tradeRole": "APP"}
    match = {"data": {"id": "t", "botTrades": [{"items": [
        {"itemDetailsId": l.extra["id"], "tradeRole": "APP"}]}]}}
    assert run(adapter(FakeHttp(HttpResult(200, match))).claim_state(None, l)) is ClaimStatus.CLAIMED
    wrong = {"data": {"id": "t", "botTrades": [{"items": [other]}]}}
    # Not found is not proof of no claim: it stays UNKNOWN, so the engine holds.
    assert run(adapter(FakeHttp(HttpResult(200, wrong))).claim_state(None, l)) is ClaimStatus.UNKNOWN


def test_reconcile_with_no_open_trade_stays_unknown():
    # After a timeout the site may still be creating the trade: never call it REJECTED.
    http = FakeHttp(BadResponse("HTTP 404", 404))
    assert run(adapter(http).claim_state(None, listing("withdraw"))) is ClaimStatus.UNKNOWN


def test_reconcile_keeps_the_trade_id():
    l = listing("withdraw")
    trade = {"data": {"id": "trade-9", "botTrades": [], "appItemsValue": str(l.price),
                      "userItemsValue": "0"}}
    state, tid = run(adapter(FakeHttp(HttpResult(200, trade))).reconcile_claim(l))
    assert state is ClaimStatus.CLAIMED and tid == "trade-9"


REFRESHED = HttpResult(200, {"success": True, "data": {"accessTokenExpiresAt": "x"}},
                       cookies={"bloxswaps.at": "NEWACCESS"})


def test_expired_cookie_refreshes_once_then_retries_with_new_cookie():
    http = FakeHttp(ME, AuthError("401"), REFRESHED, trade_ok())
    out = run(adapter(http).claim_listing(listing("withdraw"), "k"))
    assert out.status is ClaimStatus.CLAIMED
    refresh = http.calls[2]
    assert refresh["method"] == "POST" and refresh["url"] == REFRESH_URL
    retry = http.calls[3]
    assert retry["method"] == "POST" and retry["url"] == TRADES_URL
    assert "bloxswaps.at=NEWACCESS" in retry["headers"]["Cookie"]
    assert "bloxswaps.at=abc" not in retry["headers"]["Cookie"]
    assert "_ga=x" in retry["headers"]["Cookie"]  # untouched cookies are kept


def test_dead_refresh_token_rejects_without_second_claim():
    http = FakeHttp(ME, AuthError("401"), AuthError("401"))
    out = run(adapter(http).claim_listing(listing("withdraw"), "k"))
    assert out.status is ClaimStatus.REJECTED
    assert [c["url"] for c in http.calls].count(TRADES_URL) == 1  # the POST that got the 401


def test_rotated_cookies_are_saved_and_reused_after_restart(tmp_path):
    path = tmp_path / "data" / "bloxswaps_session.json"
    run(adapter_with(FakeHttp(ME, AuthError("401"), REFRESHED, trade_ok()), path)
        .claim_listing(listing("withdraw"), "k"))
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["cookies"]["bloxswaps.at"] == "NEWACCESS"

    later = FakeHttp(ME)
    a = adapter_with(later, path)
    assert "bloxswaps.at=NEWACCESS" in cookie_header(a.jar)
    run(a._read_account())
    assert "bloxswaps.at=NEWACCESS" in later.calls[0]["headers"]["Cookie"]


def test_new_token_in_env_replaces_old_saved_session(tmp_path):
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"token_sha": "not-this-token",
                                "cookies": {"bloxswaps.at": "STALE"}}), encoding="utf-8")
    a = adapter_with(FakeHttp(), path)
    assert a.jar["bloxswaps.at"] == "abc"


def test_cookie_header_round_trips():
    raw = "_ga=GA1.1.1; crisp%2Fsession=a%3Db; bloxswaps.at=eyJ.eyJ.sig"
    assert cookie_header(parse_cookie_header(raw)) == raw


def adapter_with(http, path):
    return BloxSwapsAdapter(http, token=TOKEN, session_path=path)


def test_new_trade_shape_gives_join_link_from_delivery():
    from mm2sniper.marketplaces.bloxswaps import handoff_from_trade
    trade = {"id": "t", "status": "IN_PROGRESS", "botTrades": [],
             "delivery": {"status": "COLLECTING", "bot": {"name": "bot9"},
                          "server": {"id": "srv-1"}}}
    assert handoff_from_trade(trade) == (
        "https://www.roblox.com/games/start?placeId=142823291&gameInstanceId=srv-1", "bot9")


def test_new_trade_without_legs_matches_by_value():
    from mm2sniper.marketplaces.bloxswaps import _matches_open_trade
    l = listing("withdraw")
    trade = {"id": "t", "status": "IN_PROGRESS", "botTrades": [],
             "appItemsValue": str(l.price), "userItemsValue": "0"}
    assert _matches_open_trade(trade, l)
    assert not _matches_open_trade({**trade, "appItemsValue": "999"}, l)


def test_balance_is_read_and_bad_answers_are_errors():
    from decimal import Decimal
    a = adapter(FakeHttp(HttpResult(200, {"success": True, "data": {"siteBalance": "2.98"}})))
    assert run(a.get_balance()) == Decimal("2.98")
    with pytest.raises(BadResponse):
        run(adapter(FakeHttp(HttpResult(200, {"data": {}}))).get_balance())
