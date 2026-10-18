import asyncio
from dataclasses import replace
from decimal import Decimal

from conftest import FakeAdapter, FakeNotifier, by_id, listings, make_settings, run
from mm2sniper.http import RateLimited, TransientError
from mm2sniper.service import Watcher
from mm2sniper.store import Change, Store


def make(tmp_path, mode="claim", adapter=None, **env):
    s = make_settings(tmp_path, mode, extra_env=env)
    store = Store(":memory:")
    n = FakeNotifier()
    a = adapter or FakeAdapter()
    return Watcher(s, store, a, n), store, n, a


def test_dedupe_detects_changes(store):
    before = by_id(listings("bloxswaps_before.json"), "harvester:withdraw")
    assert store.observe(before) is Change.UNAVAILABLE
    after = replace(before, available=True, quantity=1)
    assert store.observe(after) is Change.RESTOCKED
    assert store.observe(after) is Change.UNCHANGED
    assert store.observe(replace(after, price=Decimal(1))) is Change.PRICE_CHANGED
    fresh = by_id(listings("bloxswaps_before.json"), "seer:withdraw")
    assert store.observe(fresh) is Change.NEW


def test_first_sweep_primes_without_acting(tmp_path):
    w, store, n, a = make(tmp_path, "monitor")
    acted = run(w.process(listings("bloxswaps_after.json")))
    assert acted == [] and n.events == []


def test_restock_and_slot_open_trigger_in_monitor_mode(tmp_path):
    w, store, n, a = make(tmp_path, "monitor")
    run(w.process(listings("bloxswaps_before.json")))
    acted = run(w.process(listings("bloxswaps_after.json")))
    assert sorted(l.listing_id for l, _ in acted) == ["chroma-seer:deposit",
                                                      "harvester:withdraw"]
    # Icepiercer restocked but at 500 > max 200: skipped. Plain Seer is unwatched.
    assert all(e[0] == "available" for e in n.events)
    assert not a.claim_calls


def test_notifications_not_repeated_for_same_state(tmp_path):
    w, store, n, a = make(tmp_path, "monitor")
    run(w.process(listings("bloxswaps_before.json")))
    run(w.process(listings("bloxswaps_after.json")))
    run(w.process(listings("bloxswaps_after.json")))
    assert len(n.events) == 2


def test_claim_mode_claims_first_trigger_then_stops(tmp_path):
    after = listings("bloxswaps_after.json")
    w, store, n, a = make(tmp_path, adapter=FakeAdapter(after))
    run(w.process(listings("bloxswaps_before.json")))
    acted = run(w.process(after))
    assert len(acted) == 1 and acted[0][1] == "claimed"
    assert len(a.claim_calls) == 1
    assert store.get_hold() is not None


def test_first_sight_option(tmp_path):
    after = listings("bloxswaps_after.json")
    w, store, n, a = make(tmp_path, "monitor", ACT_ON_FIRST_SIGHT="true")
    acted = run(w.process(after))
    ids = {l.listing_id for l, _ in acted}
    assert "harvester:withdraw" in ids


def test_transient_skip_is_retried_next_poll(tmp_path):
    after = listings("bloxswaps_after.json")
    a = FakeAdapter(after)
    w, store, n, _ = make(tmp_path, adapter=a)
    run(w.process(listings("bloxswaps_before.json")))
    a.ls = []                                   # revalidation: listing disappeared
    run(w.process(after))
    assert not a.claim_calls and store.get_hold() is None
    again = listings("bloxswaps_after.json")    # next poll: still there, try again
    a.ls = again
    run(w.process(again))
    assert len(a.claim_calls) == 1


def test_run_loop_survives_errors_and_rate_limits(tmp_path):
    after = listings("bloxswaps_after.json")
    a = FakeAdapter(after)
    a.poll_exc = [TransientError("timeout"), RateLimited(0.01, 429), TransientError("502")]
    w, store, n, _ = make(tmp_path, "monitor", adapter=a, POLL_INTERVAL_SECONDS="10")

    async def go():
        orig = w._sleep

        async def fast(sec):          # don't actually wait in tests
            return await orig(0) or w._stop.is_set()
        w._sleep = fast
        task = asyncio.create_task(w.run())
        for _ in range(200):
            await asyncio.sleep(0)
            if a.status.polls >= 5:
                break
        w.stop()
        await asyncio.wait_for(task, 2)
    run(go())
    st = a.status
    assert st.polls >= 4 and st.reachable and st.consecutive_failures == 0
    titles = [e[1] for e in n.events if e[0] == "system"]
    assert "Rate limited by BloxSwaps" in titles
    assert w.s.status_file.exists()


def test_down_alert_after_repeated_failures(tmp_path):
    a = FakeAdapter([])
    a.poll_exc = [TransientError("x")] * 6
    w, store, n, _ = make(tmp_path, "monitor", adapter=a)

    async def go():
        orig = w._sleep

        async def fast(sec):
            return await orig(0) or w._stop.is_set()
        w._sleep = fast
        task = asyncio.create_task(w.run())
        for _ in range(300):
            await asyncio.sleep(0)
            if a.status.polls >= 7:
                break
        w.stop()
        await asyncio.wait_for(task, 2)
    run(go())
    titles = [e[1] for e in n.events if e[0] == "system"]
    assert "🔴 BloxSwaps unreachable" in titles


def test_polling_paused_while_on_hold(tmp_path):
    after = listings("bloxswaps_after.json")
    a = FakeAdapter(after, status=False)
    w, store, n, _ = make(tmp_path, adapter=a)
    run(w.process(listings("bloxswaps_before.json")))
    run(w.process(after))
    assert store.get_hold() is not None
    polls_before = a.status.polls

    async def go():
        orig = w._sleep

        async def fast(sec):
            return await orig(0) or w._stop.is_set()
        w._sleep = fast
        task = asyncio.create_task(w.run())
        for _ in range(50):
            await asyncio.sleep(0)
        w.stop()
        await asyncio.wait_for(task, 2)
    run(go())
    assert a.status.polls == polls_before          # no polling during the hold
    assert a.status.paused_reason.startswith("waiting for trade")


def test_item_released_after_start_triggers(tmp_path):
    w, store, n, a = make(tmp_path, "monitor")
    before = listings("bloxswaps_before.json")
    run(w.process(before))
    h = by_id(listings("bloxswaps_after.json"), "harvester:withdraw")
    fresh = replace(h, listing_id="mortal:withdraw", slug="mortal", name="Mortal")
    w.s.watch.append(replace(w.s.watch[0], item="Mortal", key="mortal"))
    acted = run(w.process(before + [fresh]))
    assert [l.listing_id for l, _ in acted] == ["mortal:withdraw"]
