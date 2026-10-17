"""End-to-end adapter tests: the real HttpClient talks to a local mock of the
BloxSwaps API. This checks error classification, the session refresh, and that no
cookie value reaches logs or notifications."""
import asyncio
import logging

import pytest
from aiohttp import web

from conftest import listings
from mm2sniper.http import BadResponse, HttpClient, RequestNotSent
import mm2sniper.marketplaces.bloxswaps as bx
from mm2sniper.marketplaces.bloxswaps import BloxSwapsAdapter
from mm2sniper.models import ClaimStatus

ACCESS_MARK = "ACCESSMARK111"
REFRESH_MARK = "REFRESHMARK222"
NEW_ACCESS = "NEWACCESSMARK333"
TOKEN = f"_ga=x; bloxswaps.at={ACCESS_MARK}; bloxswaps.rt={REFRESH_MARK}"


def listing(side="withdraw"):
    return next(l for l in listings("bloxswaps_before.json")
                if l.side == side and l.extra.get("id") is not None and l.available)


class Mock:
    """Server state. `at` is the access cookie the server accepts, `rt_ok` whether
    the refresh cookie is still good, `post` how POST /trades answers."""

    def __init__(self):
        self.at = ACCESS_MARK
        self.rt_ok = True
        self.post = "ok"            # ok | 500 | nonjson | 400
        self.active = None          # trade dict returned by GET /trades/active
        self.trades_created = 0
        self.seen_cookies = []

    def authed(self, req):
        cookie = req.headers.get("Cookie", "")
        self.seen_cookies.append(cookie)
        return f"bloxswaps.at={self.at}" in cookie


def make_app(m: Mock):
    app = web.Application()

    async def me(req):
        if not m.authed(req):
            return web.Response(status=401)
        return web.json_response({"success": True, "data": {"user": {
            "robloxUsername": "TestPlayer"}}})

    async def refresh(req):
        if f"bloxswaps.rt={REFRESH_MARK}" not in req.headers.get("Cookie", "") or not m.rt_ok:
            return web.Response(status=401)
        m.at = NEW_ACCESS
        resp = web.json_response({"success": True, "data": {"accessTokenExpiresAt": "x"}})
        resp.set_cookie("bloxswaps.at", NEW_ACCESS, path="/")
        return resp

    async def create(req):
        if not m.authed(req):
            return web.Response(status=401)
        if m.post == "500":
            m.trades_created += 1          # the site may still have acted
            return web.Response(status=500)
        if m.post == "nonjson":
            m.trades_created += 1
            return web.Response(text="<html>gateway</html>")
        if m.post == "400":
            return web.json_response({"success": False, "message": "no stock"}, status=400)
        m.trades_created += 1
        return web.json_response({"success": True, "data": {
            "id": "trade-int-1", "status": "IN_PROGRESS", "botTrades": [],
            "delivery": {"status": "COLLECTING", "bot": {"name": "bot7"},
                         "server": {"id": "srv-int"}},
            "appItemsValue": "0.1", "userItemsValue": "0"}})

    async def active(req):
        if not m.authed(req):
            return web.Response(status=401)
        if m.active is None:
            return web.Response(status=404)
        return web.json_response({"success": True, "data": m.active})

    app.router.add_get("/auth/me", me)
    app.router.add_post("/auth/refresh", refresh)
    app.router.add_post("/trades", create)
    app.router.add_get("/trades/active", active)
    return app


@pytest.fixture
def mock_site(monkeypatch):
    """Point the adapter at a local server. Yields (mock, base_url)."""
    holder = {}

    async def start():
        m = Mock()
        runner = web.AppRunner(make_app(m))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        monkeypatch.setattr(bx, "ME_URL", f"{base}/auth/me")
        monkeypatch.setattr(bx, "REFRESH_URL", f"{base}/auth/refresh")
        monkeypatch.setattr(bx, "TRADES_URL", f"{base}/trades")
        holder["runner"] = runner
        return m

    yield start
    # runners are cleaned up by each test's own loop


def _with_site(mock_site, body):
    """Run `body(mock, client, adapter)` against the local site, then clean up."""
    async def main():
        m = await mock_site()
        client = HttpClient(min_host_interval=0, default_timeout=5)
        adapter = BloxSwapsAdapter(client, token=TOKEN, timeout=5)
        try:
            return await body(m, client, adapter)
        finally:
            await client.close()
    return asyncio.run(main())


def test_expired_access_cookie_refreshes_and_claims(mock_site):
    async def body(m, client, a):
        # Simulate expiry: the server only accepts a new access cookie, so the first
        # try gets 401, the refresh issues the new one, and the retry succeeds.
        m.at = NEW_ACCESS
        out = await a.claim_listing(listing(), "k")
        return out, m
    out, m = _with_site(mock_site, body)
    assert out.status is ClaimStatus.CLAIMED and out.claim_id == "trade-int-1"
    assert out.server_link == ("https://www.roblox.com/games/start?placeId=142823291"
                               "&gameInstanceId=srv-int")
    assert out.bot_name == "bot7" and out.username == "TestPlayer"
    assert m.trades_created == 1
    assert any(NEW_ACCESS in c for c in m.seen_cookies)


def test_dead_refresh_token_claims_nothing(mock_site):
    async def body(m, client, a):
        m.at = NEW_ACCESS
        m.rt_ok = False
        return await a.claim_listing(listing(), "k"), m
    out, m = _with_site(mock_site, body)
    assert out.status is ClaimStatus.REJECTED
    assert m.trades_created == 0


def test_server_error_on_claim_is_unknown_and_reconciles(mock_site):
    async def body(m, client, a):
        m.post = "500"
        out = await a.claim_listing(listing(), "k")
        assert out.status is ClaimStatus.UNKNOWN
        m.active = {"id": "trade-int-1", "status": "IN_PROGRESS", "botTrades": [],
                    "appItemsValue": str(listing().price), "userItemsValue": "0"}
        return await a.claim_state(None, listing()), m
    state, m = _with_site(mock_site, body)
    assert state is ClaimStatus.CLAIMED and m.trades_created == 1


def test_unparseable_answer_to_claim_is_unknown_not_rejected(mock_site):
    # The site answered 200 but with a body we cannot read. The trade may exist, so
    # this must be UNKNOWN (reconciled), never REJECTED.
    async def body(m, client, a):
        m.post = "nonjson"
        return await a.claim_listing(listing(), "k"), m
    out, m = _with_site(mock_site, body)
    assert out.status is ClaimStatus.UNKNOWN


def test_clear_refusal_is_rejected(mock_site):
    async def body(m, client, a):
        m.post = "400"
        return await a.claim_listing(listing(), "k"), m
    out, _ = _with_site(mock_site, body)
    assert out.status is ClaimStatus.REJECTED


def test_unreachable_site_is_retryable_not_sent(monkeypatch):
    # A port that was just closed refuses at once (port 1 hangs on Windows instead).
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    monkeypatch.setattr(bx, "ME_URL", f"http://127.0.0.1:{port}/auth/me")

    async def main():
        client = HttpClient(min_host_interval=0, default_timeout=10)
        try:
            with pytest.raises(RequestNotSent):
                await BloxSwapsAdapter(client, token=TOKEN, timeout=10).claim_listing(
                    listing(), "k")
        finally:
            await client.close()
    asyncio.run(main())


def test_redirects_are_not_followed(monkeypatch):
    """A redirect must never carry our session cookie (or a claim body) elsewhere:
    it is not followed at all, and is reported as an unclear answer."""
    seen = {}

    async def main():
        other = web.Application()

        async def landing(req):
            seen["cookie"] = req.headers.get("Cookie", "")
            return web.json_response({"success": True, "data": {}})

        other.router.add_get("/landing", landing)
        origin = web.Application()

        async def redirect(req):
            raise web.HTTPFound(location=f"{seen['base']}/landing")

        origin.router.add_get("/auth/me", redirect)
        runners = []
        for app in (other, origin):
            r = web.AppRunner(app)
            await r.setup()
            s = web.TCPSite(r, "127.0.0.1", 0)
            await s.start()
            runners.append((r, s._server.sockets[0].getsockname()[1]))
        seen["base"] = f"http://127.0.0.1:{runners[0][1]}"
        client = HttpClient(min_host_interval=0, default_timeout=5)
        try:
            with pytest.raises(BadResponse) as e:
                await client.request_json("GET", f"http://127.0.0.1:{runners[1][1]}/auth/me",
                                          headers={"Cookie": TOKEN})
            seen["status"] = e.value.status
        finally:
            await client.close()
            for r, _ in runners:
                await r.cleanup()
    asyncio.run(main())
    assert seen["status"] == 302
    assert "cookie" not in seen, "the redirect target was contacted"


def test_cookie_values_never_reach_logs_or_notifications(mock_site, caplog):
    caplog.set_level(logging.DEBUG)

    async def body(m, client, a):
        m.at = NEW_ACCESS          # forces the refresh path
        out = await a.claim_listing(listing(), "k")
        m.post = "500"            # and an unclear answer
        out2 = await a.claim_listing(listing(), "k2")
        return out, out2
    _with_site(mock_site, body)
    text = caplog.text
    for mark in (ACCESS_MARK, REFRESH_MARK, NEW_ACCESS):
        assert mark not in text, f"{mark} appeared in logs"
