from decimal import Decimal

import pytest

from conftest import FIX, make_settings
from mm2sniper.config import ConfigError, load_settings


def test_defaults_are_safe(tmp_path):
    s = make_settings(tmp_path)
    assert s.mode == "monitor"
    assert s.poll_interval_seconds == 30.0
    assert not s.act_on_first_sight
    assert [w.item for w in s.watch] == ["Harvester", "C. Seer", "Icepiercer"]
    assert s.watch[0].max_price == Decimal(400)


def test_watch_matching_respects_side(tmp_path):
    s = make_settings(tmp_path)
    assert s.watch_for("Harvester", "harvester", "withdraw") is not None
    assert s.watch_for("Harvester", "harvester", "deposit") is None
    assert s.watch_for("Chroma Seer", "chroma-seer", "deposit") is not None
    assert s.watch_for("Seer", "seer", "deposit") is None   # plain != chroma


def collect(tmp_path, env, toml=None):
    cfg = None
    if toml is not None:
        cfg = tmp_path / "c.toml"
        cfg.write_text(toml, encoding="utf-8")
    with pytest.raises(ConfigError) as e:
        load_settings(cfg or FIX / "watch.toml", env=env)
    return " | ".join(e.value.problems)


def test_reports_all_problems_at_once(tmp_path):
    msg = collect(tmp_path, {"MODE": "yolo", "POLL_INTERVAL_SECONDS": "0.5",
                             "MAX_DAILY_SPEND": "lots", "DISCORD_WEBHOOK_URL": "http://evil",
                             "LOG_LEVEL": "LOUD"})
    for needle in ("MODE", "POLL_INTERVAL_SECONDS", "MAX_DAILY_SPEND", "DISCORD_WEBHOOK_URL",
                   "LOG_LEVEL"):
        assert needle in msg


def test_empty_watchlist_rejected(tmp_path):
    assert "watchlist is empty" in collect(tmp_path, {}, toml="")


def test_claim_mode_needs_webhook_and_token(tmp_path):
    msg = collect(tmp_path, {"MODE": "claim"})
    assert "DISCORD_WEBHOOK_URL" in msg and "BLOXSWAPS_TOKEN" in msg


def test_bad_watch_entries(tmp_path):
    msg = collect(tmp_path, {}, toml='[[watch]]\nitem="A"\nside="sideways"\n'
                                     '[[watch]]\nitem="B"\n[[watch]]\nitem="b"\n'
                                     '[[watch]]\nmax_price=3\n'
                                     '[[watch]]\nitem="C"\nmax_price=-1\n')
    assert "side must be" in msg and "listed twice" in msg and "needs an item" in msg
    assert "non-negative" in msg


def test_webhook_in_config_file_rejected(tmp_path):
    msg = collect(tmp_path, {}, toml='discord_webhook_url="https://discord.com/api/webhooks/1/x"'
                                     '\n[[watch]]\nitem="A"\n')
    assert "DISCORD_WEBHOOK_URL environment" in msg


def test_invalid_toml(tmp_path):
    msg = collect(tmp_path, {}, toml="[[watch]\nitem=")
    assert "not valid TOML" in msg


def test_missing_config_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "nope.toml", env={})


def test_env_watchlist(tmp_path):
    s = load_settings(None, env={"WATCH": "Harvester, C. Seer@deposit, X@both"})
    assert [(w.item, w.side) for w in s.watch] == [
        ("Harvester", "withdraw"), ("C. Seer", "deposit"), ("X", "both")]


def test_secrets_never_in_redacted_view(tmp_path):
    s = make_settings(tmp_path, "claim")
    shown = str(s.redacted())
    assert "test-token-not-real" not in shown and "abc-DEF_123" not in shown
    assert "BLOXSWAPS_TOKEN" in shown  # only the name


def test_watch_env_min_quantity(tmp_path):
    from mm2sniper.config import WATCH_ENTRY_RE
    m = WATCH_ENTRY_RE.match("Harvester<=330 x3")
    assert m and m["item"] == "Harvester" and m["qty"] == "3" and "330" in m["limits"]
    assert WATCH_ENTRY_RE.match("Harvester")["qty"] is None
