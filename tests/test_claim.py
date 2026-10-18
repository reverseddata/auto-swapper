import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from conftest import FakeAdapter, FakeNotifier, by_id, listings, make_settings, run
from mm2sniper.claim import ClaimEngine
from mm2sniper.http import RequestNotSent, TransientError
from mm2sniper.models import ClaimOutcome, ClaimStatus
from mm2sniper.store import Hold, Store


def setup(tmp_path, adapter, **env):
    s = make_settings(tmp_path, "claim", extra_env=env)
    store = Store(":memory:")
    n = FakeNotifier()
    return s, store, n, ClaimEngine(s, store, adapter, n)


def harvester():
    return by_id(listings("bloxswaps_after.json"), "harvester:withdraw")


def test_successful_claim_sets_hold_and_pings(tmp_path):
    l = harvester()
    a = FakeAdapter([l])
    s, store, n, eng = setup(tmp_path, a)
    out = run(eng.attempt(l, s.watch[0]))
    assert out.status is ClaimStatus.CLAIMED
    assert len(a.claim_calls) == 1
    assert store.get_hold().name == "Harvester"
    assert n.kinds() == ["claimed"]
    row = store.recent_claims()[0]
    assert row["status"] == "claimed" and "roblox.com" in row["handoff"]


def test_no_second_claim_while_on_hold(tmp_path):
    l = harvester()
    a = FakeAdapter([l])
    s, store, n, eng = setup(tmp_path, a)
    run(eng.attempt(l, s.watch[0]))
    out = run(eng.attempt(replace(l, detected_at=datetime.now(timezone.utc)), s.watch[0]))
    assert out.status is ClaimStatus.SKIPPED and len(a.claim_calls) == 1


def test_concurrent_attempts_claim_once(tmp_path):
    l = harvester()
    a = FakeAdapter([l], claim_delay=0.05)
    s, store, n, eng = setup(tmp_path, a)

    async def go():
        return await asyncio.gather(*(eng.attempt(l, s.watch[0]) for _ in range(10)))
    outs = run(go())
    assert len(a.claim_calls) == 1
    assert sum(o.status is ClaimStatus.CLAIMED for o in outs) == 1


def test_duplicate_idempotency_key_rejected(store):
    l = harvester()
    assert store.reserve_claim(idem_key="k", listing=l, daily_limit=None).ok
    store.update_claim("k", ClaimStatus.REJECTED)
    r = store.reserve_claim(idem_key="k", listing=l, daily_limit=None)
    assert not r.ok and "duplicate" in r.reason


def test_daily_cap_blocks_and_alerts_once(tmp_path):
    l = harvester()   # price 312.9
    a = FakeAdapter([l], claim_outcomes=[ClaimOutcome(ClaimStatus.REJECTED, detail="sold")])
    s, store, n, eng = setup(tmp_path, a, MAX_DAILY_SPEND="400")
    run(eng.attempt(l, s.watch[0]))          # rejected -> not counted
    assert store.spent() == 0
    store.reserve_claim(idem_key="old", listing=l, daily_limit=None)
    store.update_claim("old", ClaimStatus.COMPLETED)   # 312.9 spent today
    for i in range(2):
        out = run(eng.attempt(replace(l, detected_at=datetime.now(timezone.utc)
                                      + timedelta(seconds=i + 1)), s.watch[0]))
        assert out.status is ClaimStatus.SKIPPED and "daily cap" in out.detail
    assert [e[1] for e in n.events if e[0] == "system"] == ["Daily spend cap reached"]


def test_deposits_do_not_count_toward_spend(store):
    dep = by_id(listings("bloxswaps_after.json"), "chroma-seer:deposit")
    store.reserve_claim(idem_key="d", listing=dep, daily_limit=Decimal(1))
    store.update_claim("d", ClaimStatus.COMPLETED)
    assert store.spent() == 0


def test_revalidation_failure_skips_without_claiming(tmp_path):
    l = harvester()
    gone = replace(l, available=False, quantity=0)
    a = FakeAdapter([gone])
    s, store, n, eng = setup(tmp_path, a)
    out = run(eng.attempt(l, s.watch[0]))
    assert out.status is ClaimStatus.SKIPPED and not a.claim_calls
    assert store.get_hold() is None


def test_price_rise_caught_at_revalidation(tmp_path):
    l = harvester()
    a = FakeAdapter([replace(l, price=Decimal(999))])
    s, store, n, eng = setup(tmp_path, a)
    out = run(eng.attempt(l, s.watch[0]))
    assert out.status is ClaimStatus.SKIPPED and "price" in out.detail and not a.claim_calls


def test_timeout_is_unknown_reconciled_and_never_retried(tmp_path):
    l = harvester()
    a = FakeAdapter([l], claim_exc=[TransientError("timeout")],
                    claim_states=[ClaimStatus.UNKNOWN])
    s, store, n, eng = setup(tmp_path, a)
    out = run(eng.attempt(l, s.watch[0]))
    assert out.status is ClaimStatus.UNKNOWN
    assert len(a.claim_calls) == 1               # no blind retry
    assert store.get_hold() is not None          # paused to be safe
    assert n.kinds() == ["failed"]


def test_unknown_reconciled_to_claimed(tmp_path):
    l = harvester()
    a = FakeAdapter([l], claim_exc=[TransientError("reset")], claim_states=[ClaimStatus.CLAIMED])
    a.account = "MyAlt"   # the adapter reads this from its session
    s, store, n, eng = setup(tmp_path, a)
    out = run(eng.attempt(l, s.watch[0]))
    assert out.status is ClaimStatus.CLAIMED and out.username == "MyAlt"
    assert n.kinds() == ["claimed"]


def test_unknown_reconciled_to_not_claimed(tmp_path):
    l = harvester()
    a = FakeAdapter([l], claim_exc=[TransientError("reset")], claim_states=[ClaimStatus.EXPIRED])
    s, store, n, eng = setup(tmp_path, a)
    out = run(eng.attempt(l, s.watch[0]))
    assert out.status is ClaimStatus.REJECTED and store.get_hold() is None


def test_request_not_sent_is_retried_safely(tmp_path):
    l = harvester()
    a = FakeAdapter([l], claim_exc=[RequestNotSent("dns")])
    s, store, n, eng = setup(tmp_path, a)
    out = run(eng.attempt(l, s.watch[0]))
    assert out.status is ClaimStatus.CLAIMED and len(a.claim_calls) == 2


def test_hold_resumes_when_site_reports_completed(tmp_path):
    l = harvester()
    a = FakeAdapter([l], claim_states=[ClaimStatus.CLAIMED, ClaimStatus.COMPLETED])
    s, store, n, eng = setup(tmp_path, a)
    run(eng.attempt(l, s.watch[0]))
    assert run(eng.check_hold()) is True          # still waiting
    assert run(eng.check_hold()) is False         # finished
    assert store.get_hold() is None
    assert store.recent_claims()[0]["status"] == "completed"
    assert n.events[-1][1] == "▶️ Polling resumed"


def test_hold_times_out(tmp_path):
    l = harvester()
    a = FakeAdapter([l], status=False)
    s, store, n, eng = setup(tmp_path, a)
    run(eng.attempt(l, s.watch[0]))
    h = store.get_hold()
    store.set_hold(Hold(h.idem_key, h.name, h.since, datetime.now(timezone.utc)
                        - timedelta(seconds=1)))
    assert run(eng.check_hold()) is False
    # No final answer: recorded as timed out, and it still counts toward the daily cap.
    assert store.recent_claims()[0]["status"] == "timed_out"
    assert "timed out" in n.events[-1][2]


def test_crash_mid_claim_is_reconciled_at_startup(tmp_path):
    l = harvester()
    a = FakeAdapter([l])
    s, store, n, eng = setup(tmp_path, a)
    store.reserve_claim(idem_key="crashed", listing=l, daily_limit=None)   # PENDING
    eng.reconcile_on_startup()
    assert store.claim("crashed")["status"] == "unknown"
    assert store.get_hold() is not None
    assert run(eng.attempt(l, s.watch[0])).status is ClaimStatus.SKIPPED
    assert not a.claim_calls


def test_unsupported_adapter_never_claims(tmp_path):
    l = harvester()
    a = FakeAdapter([l], claim=False)
    s, store, n, eng = setup(tmp_path, a)
    out = run(eng.attempt(l, s.watch[0]))
    assert out.status is ClaimStatus.SKIPPED and not a.claim_calls


def _claimed_qty(a):
    return a.claimed[0].quantity


def test_withdraw_takes_all_stock_within_balance(tmp_path):
    l = replace(harvester(), quantity=3)   # price 312.9
    a = FakeAdapter([l])
    a.get_balance = lambda: asyncio.sleep(0, Decimal("700"))   # pays for 2
    s, store, n, eng = setup(tmp_path, a)
    assert run(eng.attempt(l, s.watch[0])).status is ClaimStatus.CLAIMED
    assert _claimed_qty(a) == 2


def test_withdraw_quantity_capped_by_setting(tmp_path):
    l = replace(harvester(), quantity=3)
    a = FakeAdapter([l])
    s, store, n, eng = setup(tmp_path, a, MAX_CLAIM_QUANTITY="1")
    run(eng.attempt(l, s.watch[0]))
    assert _claimed_qty(a) == 1


def test_min_quantity_waits_for_enough_stock(tmp_path):
    l = replace(harvester(), quantity=2)
    a = FakeAdapter([l])
    s, store, n, eng = setup(tmp_path, a)
    w = replace(s.watch[0], min_qty=3)
    out = run(eng.attempt(l, w))
    assert out.status is ClaimStatus.SKIPPED and "minimum is 3" in out.detail
    assert a.claimed == []


def test_min_quantity_skips_when_balance_too_low(tmp_path):
    l = replace(harvester(), quantity=3)   # price 312.9
    a = FakeAdapter([l])
    a.get_balance = lambda: asyncio.sleep(0, Decimal("700"))   # pays for 2
    s, store, n, eng = setup(tmp_path, a)
    out = run(eng.attempt(l, replace(s.watch[0], min_qty=3)))
    assert out.status is ClaimStatus.SKIPPED and a.claimed == []


def test_min_quantity_claims_when_met(tmp_path):
    l = replace(harvester(), quantity=3)
    a = FakeAdapter([l])
    s, store, n, eng = setup(tmp_path, a)
    assert run(eng.attempt(l, replace(s.watch[0], min_qty=3))).status is ClaimStatus.CLAIMED
    assert a.claimed[0].quantity == 3
