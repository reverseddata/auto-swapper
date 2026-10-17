import asyncio

from conftest import by_id, listings, run
from mm2sniper.models import ClaimOutcome, ClaimStatus
from mm2sniper.notify import (DiscordNotifier, available_embed, claim_embed, claim_failed_embed,
                              redact)

URL = "https://discord.com/api/webhooks/1/abc"


class FakeResp:
    def __init__(self, status, body=None, headers=None):
        self.status, self._body, self.headers = status, body or {}, headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._body

    async def text(self):
        return str(self._body)


class FakeSession:
    closed = False

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.posts = []

    def post(self, url, json=None, timeout=None):
        self.posts.append(json)
        st = self.statuses.pop(0) if self.statuses else 204
        if st == 429:
            return FakeResp(429, {"retry_after": 0.01})
        return FakeResp(st)

    async def close(self):
        pass


def claimed():
    l = by_id(listings("bloxswaps_after.json"), "harvester:withdraw")
    out = ClaimOutcome(ClaimStatus.CLAIMED, "c-9", server_link="https://www.roblox.com/s/x",
                       username="MyAlt", bot_name="boxswap77")
    return l, out


def test_claim_embed_says_item_link_and_username():
    l, out = claimed()
    e = claim_embed(l, out)
    assert e["title"] == "✅ Harvester claimed"
    assert "https://www.roblox.com/s/x" in e["description"] and "**MyAlt**" in e["description"]
    names = {f["name"]: f["value"] for f in e["fields"]}
    assert names["Price"] == "312.9 BloxSwaps value" and names["Bot"] == "boxswap77"
    assert names["Claim ID"] == "`c-9`"
    assert len(e["title"]) <= 256 and all(len(f["value"]) <= 1024 for f in e["fields"])


def test_deposit_embed_title():
    l = by_id(listings("bloxswaps_after.json"), "chroma-seer:deposit")
    assert claim_embed(l, ClaimOutcome(ClaimStatus.CLAIMED))["title"] == \
        "✅ Chroma Seer deposit slot claimed"
    assert "deposit slot open" in available_embed(l, "monitor")["title"]


def test_failed_embed_never_leaks_tokens():
    l, _ = claimed()
    out = ClaimOutcome(ClaimStatus.UNKNOWN,
                       detail="Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY.abc "
                              "https://discord.com/api/webhooks/1/secret token=hunter2")
    text = str(claim_failed_embed(l, out))
    assert "eyJhbGci" not in text and "secret" not in text and "hunter2" not in text


def test_redact():
    assert "abc" not in redact("Bearer abc.def")
    assert redact("cookie=xyz; other") == "cookie=[redacted]; other"


def _notifier(statuses, **kw):
    sess = FakeSession(statuses)
    n = DiscordNotifier(URL, session_factory=lambda: sess, cooldown_seconds=60, **kw)
    return n, sess


def test_batches_and_cooldown():
    async def t():
        n, sess = _notifier([])
        n.start()
        for i in range(12):
            n.system(f"t{i}", "m")
        assert n.post({"title": "x"}, key="k") and not n.post({"title": "x"}, key="k")
        await n.stop()
        return n, sess
    n, sess = run(t())
    assert n.sent == 13
    assert all(len(p["embeds"]) <= 10 for p in sess.posts) and len(sess.posts) >= 2


def test_rate_limit_and_server_errors_are_retried():
    async def t():
        n, sess = _notifier([429, 500, 204])
        n.start()
        n.system("a", "b")
        await n.stop()
        return n, sess
    n, sess = run(t())
    assert n.sent == 1 and len(sess.posts) == 3


def test_rejected_message_does_not_crash_worker():
    async def t():
        n, sess = _notifier([400, 204])
        n.start()
        n.system("bad", "x")
        await asyncio.sleep(0.05)
        n.system("good", "y")
        await n.stop()
        return n
    n = run(t())
    assert n.failed == 1 and n.sent == 1


def test_claim_pings_mention():
    async def t():
        n, sess = _notifier([], mention="<@42>")
        n.start()
        n.claim_succeeded(*claimed())
        await n.stop()
        return sess
    assert run(t()).posts[0]["content"] == "<@42>"


def test_disabled_notifier_is_noop():
    n = DiscordNotifier("")
    assert not n.enabled and not n.post({"title": "x"})
