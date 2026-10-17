import asyncio
import time

import pytest
from aiohttp import web

from conftest import run
from mm2sniper.http import (AuthError, BadResponse, HttpClient, RateLimited, RequestNotSent,
                            TransientError, parse_retry_after)


async def _with_server(fn):
    app = web.Application()

    async def ok(req):
        if req.headers.get("If-None-Match") == '"v1"':
            return web.Response(status=304)
        return web.json_response({"data": [1]}, headers={"ETag": '"v1"'})

    async def limited(req):
        return web.Response(status=429, headers={"Retry-After": "42"})

    async def forbidden(req):
        return web.Response(status=403)

    async def unauth(req):
        return web.Response(status=401)

    async def boom(req):
        return web.Response(status=502)

    async def html(req):
        return web.Response(text="<html>nope</html>")

    async def slow(req):
        await asyncio.sleep(2)
        return web.json_response({})

    for path, h in [("/ok", ok), ("/429", limited), ("/403", forbidden), ("/401", unauth),
                    ("/502", boom), ("/html", html), ("/slow", slow)]:
        app.router.add_get(path, h)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    client = HttpClient(min_host_interval=0, default_timeout=0.5)
    try:
        await fn(client, f"http://127.0.0.1:{port}")
    finally:
        await client.close()
        await runner.cleanup()


def test_ok_and_etag():
    async def t(c, base):
        r = await c.request_json("GET", base + "/ok")
        assert r.data == {"data": [1]} and r.etag == '"v1"'
        r2 = await c.request_json("GET", base + "/ok", etag=r.etag)
        assert r2.not_modified
    run(_with_server(t))


@pytest.mark.parametrize("path,exc", [("/429", RateLimited), ("/403", RateLimited),
                                      ("/401", AuthError), ("/502", TransientError),
                                      ("/html", BadResponse), ("/slow", TransientError)])
def test_error_classification(path, exc):
    async def t(c, base):
        with pytest.raises(exc) as e:
            await c.request_json("GET", base + path)
        if path == "/429":
            assert e.value.retry_after == 42
    run(_with_server(t))


def test_connect_failure_is_request_not_sent():
    async def t():
        c = HttpClient(min_host_interval=0, default_timeout=15)  # Windows retries SYNs ~2s
        try:
            with pytest.raises(RequestNotSent):
                await c.request_json("GET", "http://127.0.0.1:1/x")
        finally:
            await c.close()
    run(t())


def test_per_host_spacing():
    async def t(c, base):
        c.min_host_interval = 0.2
        t0 = time.monotonic()
        for _ in range(3):
            await c.request_json("GET", base + "/ok")
        assert time.monotonic() - t0 >= 0.39
    run(_with_server(t))


@pytest.mark.parametrize("v,expected", [(None, 60), ("", 60), ("5", 5), ("0", 1),
                                        ("99999", 900), ("garbage", 60)])
def test_parse_retry_after(v, expected):
    assert parse_retry_after(v) == expected


def test_response_cookies_are_returned():
    app = web.Application()

    async def refresh(req):
        resp = web.json_response({"data": {}})
        resp.set_cookie("bloxswaps.at", "NEWTOKEN", path="/", httponly=True)
        return resp

    app.router.add_post("/refresh", refresh)

    async def run_it():
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        c = HttpClient(min_host_interval=0, default_timeout=2)
        try:
            r = await c.request_json("POST", f"http://127.0.0.1:{port}/refresh")
            assert r.cookies == {"bloxswaps.at": "NEWTOKEN"}
        finally:
            await c.close()
            await runner.cleanup()

    run(run_it())
