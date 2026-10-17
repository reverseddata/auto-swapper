"""Regression tests for the security audit findings."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from conftest import FakeAdapter, FakeNotifier, by_id, listings, make_settings, run
from mm2sniper.claim import ClaimEngine
from mm2sniper.config import ConfigError, load_settings
from mm2sniper.lock import InstanceLock
from mm2sniper.logging_setup import setup_logging
from mm2sniper.marketplaces.bloxswaps import handoff_from_trade, normalize_rows
from mm2sniper.models import ClaimOutcome, ClaimStatus
from mm2sniper.notify import DiscordNotifier, claim_embed, redact, register_secret
from mm2sniper.store import Hold, Store

D = Decimal


def harvester():
    return by_id(listings("bloxswaps_after.json"), "harvester:withdraw")


# ---------------------------------------------------------------- reconcile / hold


def test_timeout_keeps_hold_while_site_shows_trade_open(tmp_path):
    l = harvester()
    a = FakeAdapter([l], claim_states=[ClaimStatus.CLAIMED])
    s = make_settings(tmp_path, "claim")
    store, n = Store(":memory:"), FakeNotifier()
    eng = ClaimEngine(s, store, a, n)
    run(eng.attempt(l, s.watch[0]))
    h = store.get_hold()
    store.set_hold(Hold(h.idem_key, h.name, h.since,
                        datetime.now(timezone.utc) - timedelta(seconds=1)))
    assert run(eng.check_hold()) is True          # still open on the site: keep holding
    assert store.get_hold() is not None
    assert store.recent_claims()[0]["status"] == "claimed"


def test_unclear_claim_is_found_on_the_site_and_followed(tmp_path):
    l = harvester()
    from mm2sniper.http import TransientError
    # The claim answer is lost (UNKNOWN, no trade id); reconcile still sees nothing yet.
    a = FakeAdapter([l], claim_exc=[TransientError("reset")],
                    claim_states=[ClaimStatus.UNKNOWN, ClaimStatus.CLAIMED])
    found = []

    async def reconcile(listing):
        state = await a.claim_state(None, listing)
        found.append(listing.extra.get("id"))
        return state, ("trade-77" if state is ClaimStatus.CLAIMED else None)
    a.reconcile_claim = reconcile
    s = make_settings(tmp_path, "claim")
    store = Store(":memory:")
    eng = ClaimEngine(s, store, a, FakeNotifier())
    assert run(eng.attempt(l, s.watch[0])).status is ClaimStatus.UNKNOWN
    assert run(eng.check_hold()) is True        # the hold now finds the trade on the site
    row = store.recent_claims()[0]
    assert row["status"] == "claimed" and row["claim_id"] == "trade-77"
    assert found[-1] == l.extra.get("id")


def test_timed_out_claim_still_counts_toward_daily_cap(tmp_path, store):
    l = harvester()
    assert store.reserve_claim(idem_key="a", listing=l, daily_limit=None).ok
    store.update_claim("a", ClaimStatus.TIMED_OUT)
    assert store.spent() == l.price


def test_claim_price_moved_raises_an_alert(tmp_path):
    l = harvester()
    over = ClaimOutcome(ClaimStatus.CLAIMED, "t1", "ok", charged=l.price + 50)
    a = FakeAdapter([l], claim_outcomes=[over])
    s = make_settings(tmp_path, "claim")
    n = FakeNotifier()
    run(ClaimEngine(s, Store(":memory:"), a, n).attempt(l, s.watch[0]))
    assert any(e[0] == "system" and "price moved" in e[1] for e in n.events)


# ---------------------------------------------------------------- config / prices


def _env(tmp_path, **kw):
    env = {"MODE": "claim", "DATA_DIR": str(tmp_path), "LOG_DIR": str(tmp_path),
           "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/abc-DEF_123",
           "BLOXSWAPS_TOKEN": "a=b"}
    env.update(kw)
    return env


def test_watch_env_accepts_price_limits(tmp_path):
    s = load_settings(None, env=_env(tmp_path, WATCH="Harvester<=330, C. Seer@deposit>=30, "
                                                     "Icepiercer@both<=50>=10"))
    by = {w.item: w for w in s.watch}
    assert by["Harvester"].max_price == D(330) and by["Harvester"].side == "withdraw"
    assert by["C. Seer"].min_price == D(30) and by["C. Seer"].side == "deposit"
    assert by["Icepiercer"].max_price == D(50) and by["Icepiercer"].min_price == D(10)


def test_claim_mode_refuses_unlimited_entries(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_settings(None, env=_env(tmp_path, WATCH="Harvester, C. Seer@deposit"))
    text = " ".join(e.value.problems)
    assert "withdraw at any price" in text and "deposit for any payout" in text
    # A global cap is enough for withdrawals.
    load_settings(None, env=_env(tmp_path, WATCH="Harvester", MAX_CLAIM_PRICE="100"))


def test_max_price_zero_is_an_error_not_no_limit(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_settings(None, env=_env(tmp_path, WATCH="Harvester<=0", MAX_CLAIM_PRICE="5"))
    assert "is 0" in " ".join(e.value.problems)


def test_bad_watch_syntax_is_reported(tmp_path):
    with pytest.raises(ConfigError):
        load_settings(None, env=_env(tmp_path, WATCH="Harvester<330", MAX_CLAIM_PRICE="5"))


# ---------------------------------------------------------------- redaction


def test_registered_secrets_are_scrubbed_in_any_format():
    register_secret("70c76af4670ff7b8deadbeefcafe")
    register_secret("s%3Asecretsid.signature")
    text = ('{"bloxswaps.rt": "70c76af4670ff7b8deadbeefcafe", "sid": "s:secretsid.signature"}'
            " repr {'bloxswaps.at': 'x'}")
    out = redact(text)
    assert "70c76af4670ff7b8deadbeefcafe" not in out
    assert "secretsid" not in out
    assert "'bloxswaps.at': 'x'" not in out


def test_hex_ids_in_links_are_not_redacted():
    sid = "0123456789abcdef0123456789abcdef"
    link = f"https://www.roblox.com/games/start?placeId=1&gameInstanceId={sid}"
    assert redact(link) == link


def test_item_and_bot_fields_are_escaped():
    from dataclasses import replace
    l = replace(harvester(), name="[Claim](https://evil.example)")
    o = ClaimOutcome(ClaimStatus.CLAIMED, "t1", "ok", bot_name="[x](https://evil.example)")
    e = claim_embed(l, o)
    for f in e["fields"]:
        assert "](https://evil.example)" not in f["value"], f


def test_json_and_hex_cookie_forms_are_redacted():
    out = redact('{"bloxswaps.rt": "' + "ab" * 40 + '", "bloxswaps.sid": "s%3Aabc.def"}')
    assert "ab" * 40 not in out and "s%3Aabc" not in out


def test_tracebacks_are_redacted_on_console(capsys):
    setup_logging("INFO")
    log = logging.getLogger("leaktest")
    try:
        raise RuntimeError("boom https://discord.com/api/webhooks/1/SECRETTOKENVALUE "
                           "bloxswaps.rt=abcdef0123456789")
    except RuntimeError:
        log.exception("failed")
    err = capsys.readouterr().err
    assert "SECRETTOKENVALUE" not in err and "abcdef0123456789" not in err
    assert "[redacted]" in err


# ---------------------------------------------------------------- Discord safety


def test_claim_embed_url_is_https_and_names_are_escaped():
    l = harvester()
    o = ClaimOutcome(ClaimStatus.CLAIMED, "t1", "ok",
                     server_link="https://www.roblox.com/games/start?placeId=1&gameInstanceId=x",
                     username="me", bot_name="evil](https://phish.example)")
    e = claim_embed(l, o)
    assert e["url"].startswith("https://")
    assert "](https://phish.example)" not in e["description"]


def test_non_https_join_link_is_not_linked():
    o = ClaimOutcome(ClaimStatus.CLAIMED, "t1", "ok", server_link="roblox://placeId=1")
    e = claim_embed(harvester(), o)
    assert "roblox://" not in e["description"] and e["url"].startswith("https://")


def test_crafted_server_id_and_join_url_are_rejected():
    bad_id = {"delivery": {"server": {"id": "x) [click](https://evil"}, "bot": {"name": "b"}}}
    assert handoff_from_trade(bad_id)[0] == ""
    bad_url = {"botTrades": [{"status": "IN_PROGRESS", "joinUrl": "https://evil.example/p"}]}
    assert handoff_from_trade(bad_url) == ("", "")
    assert handoff_from_trade({"delivery": "garbage"}) == ("", "")


def test_rejected_batch_is_resent_one_by_one():
    posted = []

    class Resp:
        def __init__(self, status):
            self.status = status
            self.headers = {}

        async def text(self):
            return "bad embed"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Session:
        closed = False

        def post(self, url, json, timeout):
            posted.append(len(json["embeds"]))
            bad = any(e.get("title") == "bad" for e in json["embeds"])
            return Resp(400 if bad else 204)

        async def close(self):
            pass

    async def go():
        n = DiscordNotifier("https://discord.com/api/webhooks/1/x", session_factory=Session)
        from mm2sniper.notify import _Msg
        await n._deliver([_Msg({"title": "ok"}), _Msg({"title": "bad"}), _Msg({"title": "ok2"})])
        return n
    n = asyncio.run(go())
    assert posted == [3, 1, 1, 1]
    assert n.sent == 2 and n.failed == 1


def test_only_the_configured_user_can_be_pinged():
    sent = {}

    class Resp:
        status = 204
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Session:
        closed = False

        def post(self, url, json, timeout):
            sent.update(json)
            return Resp()

    async def go():
        from mm2sniper.notify import _Msg
        n = DiscordNotifier("https://discord.com/api/webhooks/1/x", mention="<@123>",
                            session_factory=Session)
        await n._deliver([_Msg({"title": "x"}, content="<@123>")])
    asyncio.run(go())
    assert sent["allowed_mentions"] == {"parse": [], "users": ["123"]}


# ---------------------------------------------------------------- misc


def test_infinite_quantity_row_is_skipped_not_fatal():
    payload = {"data": [{"slug": "a", "currentPrices": {"sellPrice": "1", "buyPrice": "1"},
                         "appAvailableQuantity": float("inf")},
                        {"slug": "b", "currentPrices": {"sellPrice": "1", "buyPrice": "1"},
                         "appAvailableQuantity": 1}]}
    ls, bad = normalize_rows(payload)
    assert bad == 1 and {l.slug for l in ls} == {"b"}


def test_second_instance_cannot_take_the_lock(tmp_path):
    a, b = InstanceLock(tmp_path / "x.lock"), InstanceLock(tmp_path / "x.lock")
    assert a.acquire()
    try:
        assert not b.acquire()
    finally:
        a.release()
    assert b.acquire()
    b.release()


# ---------------------------------------------------------------- hand-off (found on a real claim)


class LateHandoffAdapter(FakeAdapter):
    """Like the real site: the create answer has no link; it appears on a later read."""
    supports_handoff = True

    def __init__(self, ls, links):
        super().__init__(ls, claim_outcomes=[ClaimOutcome(ClaimStatus.CLAIMED, "trade-1",
                                                           "created")])
        self.links = list(links)

    async def fetch_handoff(self, claim_id):
        return self.links.pop(0) if self.links else ("", "")


def test_claim_alert_waits_for_the_join_link(tmp_path, monkeypatch):
    import mm2sniper.claim as claim_mod
    monkeypatch.setattr(claim_mod, "HANDOFF_DELAY", 0)
    l = harvester()
    link = "https://www.roblox.com/games/start?placeId=142823291&gameInstanceId=srv"
    a = LateHandoffAdapter([l], links=[("", ""), (link, "bot1")])
    s = make_settings(tmp_path, "claim")
    n = FakeNotifier()
    out = run(ClaimEngine(s, Store(":memory:"), a, n).attempt(l, s.watch[0]))
    assert out.status is ClaimStatus.CLAIMED
    assert out.server_link == link and out.bot_name == "bot1"
    claimed = [e for e in n.events if e[0] == "claimed"]
    assert len(claimed) == 1 and claimed[0][2].server_link == link


def test_late_join_link_is_sent_as_a_follow_up(tmp_path, monkeypatch):
    import mm2sniper.claim as claim_mod
    monkeypatch.setattr(claim_mod, "HANDOFF_DELAY", 0)
    monkeypatch.setattr(claim_mod, "HANDOFF_TRIES", 1)
    l = harvester()
    link = "https://www.roblox.com/games/start?placeId=142823291&gameInstanceId=srv"
    a = LateHandoffAdapter([l], links=[("", ""), (link, "bot1")])
    a.claim_states = [ClaimStatus.CLAIMED]
    s = make_settings(tmp_path, "claim")
    n, store = FakeNotifier(), Store(":memory:")
    eng = ClaimEngine(s, store, a, n)
    assert run(eng.attempt(l, s.watch[0])).server_link == ""
    assert run(eng.check_hold()) is True
    claimed = [e for e in n.events if e[0] == "claimed"]
    assert len(claimed) == 2 and claimed[1][2].server_link == link
    import json as _json
    assert _json.loads(store.recent_claims()[0]["handoff"])["server_link"] == link
