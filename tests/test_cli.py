from conftest import FIX
from mm2sniper.cli import main
from mm2sniper.models import ClaimStatus
from mm2sniper.store import Hold, Store


def test_dry_run_shows_decisions_without_network(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    main(["-c", str(FIX / "watch.toml"), "dry-run", str(FIX / "bloxswaps_before.json"),
          str(FIX / "bloxswaps_after.json")])
    out = capsys.readouterr().out
    assert "Harvester" in out and "WOULD CLAIM, then pause polling" in out
    assert "polling paused for Harvester" in out
    assert "skip (price limit)" in out


def test_resume_clears_hold(capsys, monkeypatch, tmp_path):
    from datetime import datetime, timezone
    from conftest import by_id, listings
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MODE", "monitor")
    st = Store(tmp_path / "data" / "mm2sniper.sqlite3")
    l = by_id(listings("bloxswaps_after.json"), "harvester:withdraw")
    st.reserve_claim(idem_key="k", listing=l, daily_limit=None)
    st.update_claim("k", ClaimStatus.CLAIMED)
    st.set_hold(Hold("k", "Harvester", datetime.now(timezone.utc), None))
    st.close()
    main(["-c", str(FIX / "watch.toml"), "resume"])
    assert "hold for Harvester cleared" in capsys.readouterr().out
    st = Store(tmp_path / "data" / "mm2sniper.sqlite3")
    assert st.get_hold() is None and st.claim("k")["status"] == "completed"
    st.close()
