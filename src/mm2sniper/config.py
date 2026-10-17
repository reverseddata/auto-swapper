"""Configuration: secrets and headline settings from environment variables (.env),
the watchlist from a TOML file. Validated at startup; every problem is
reported at once.

Precedence: environment variable > config file > default.
Prices are in the marketplace's own unit (BloxSwaps: site value / balance).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from .models import norm_name

MODES = ("monitor", "claim")
MIN_POLL_INTERVAL = 10.0
WEBHOOK_RE = re.compile(
    r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class ConfigError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("invalid configuration:\n  - " + "\n  - ".join(problems))


SIDES = ("withdraw", "deposit", "both")
# "Item", "Item@deposit", "Item<=330", "Item@both<=50>=10"
WATCH_ENTRY_RE = re.compile(r"^(?P<item>[^@<>=]+?)\s*(?:@\s*(?P<side>[A-Za-z]+))?\s*"
                            r"(?P<limits>(?:(?:<=|>=)\s*[^<>=\s]+\s*)*)$")


@dataclass(frozen=True)
class WatchEntry:
    item: str                       # as written by the operator
    side: str = "withdraw"          # withdraw = bot restocked it; deposit = a slot opened
    max_price: Decimal | None = None  # withdraw: claim only at or below this
    min_price: Decimal | None = None  # deposit: claim only if they pay at least this
    key: str = ""                   # normalized name or slug used for matching

    def matches(self, name: str, slug: str, side: str) -> bool:
        return (self.side in (side, "both")
                and self.key in (norm_name(name), norm_name(slug)))

    def price_ok(self, side: str, price: Decimal) -> bool:
        if side == "withdraw":
            return self.max_price is None or price <= self.max_price
        return self.min_price is None or price >= self.min_price


@dataclass
class Settings:
    mode: str = "monitor"
    data_dir: Path = Path("data")
    log_dir: Path = Path("logs")
    log_level: str = "INFO"
    log_json: bool = True

    discord_webhook_url: str = ""
    discord_username: str = "MM2 Sniper"
    discord_mention: str = ""                   # "<@id>" pinged on claims
    alert_cooldown_seconds: float = 900.0

    poll_interval_seconds: float = 30.0
    request_timeout_seconds: float = 20.0
    watch: list[WatchEntry] = field(default_factory=list)
    max_claim_price: Decimal | None = None      # global cap, None = off
    max_daily_spend: Decimal | None = None      # None = off
    claim_hold_timeout_minutes: float = 30.0    # 0 = wait for `mm2sniper resume`
    claim_status_interval_seconds: float = 30.0
    act_on_first_sight: bool = False            # claim items already available at startup

    # Raw BloxSwaps session secrets, read from BLOXSWAPS_* env vars. Never logged.
    bloxswaps_secrets: dict[str, str] = field(default_factory=dict, repr=False)
    config_file: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mm2sniper.sqlite3"

    @property
    def status_file(self) -> Path:
        return self.data_dir / "status.json"

    def watch_for(self, name: str, slug: str, side: str) -> WatchEntry | None:
        return next((w for w in self.watch if w.matches(name, slug, side)), None)

    def redacted(self) -> dict:
        return {
            "mode": self.mode,
            "discord_webhook": "set" if self.discord_webhook_url else "not set",
            "poll_interval_seconds": self.poll_interval_seconds,
            "watch": [{"item": w.item, "side": w.side,
                       "max_price": str(w.max_price) if w.max_price else None,
                       "min_price": str(w.min_price) if w.min_price else None}
                      for w in self.watch],
            "max_claim_price": str(self.max_claim_price) if self.max_claim_price else None,
            "max_daily_spend": str(self.max_daily_spend) if self.max_daily_spend else None,
            "claim_hold_timeout_minutes": self.claim_hold_timeout_minutes,
            "bloxswaps_secrets": sorted(self.bloxswaps_secrets) or "none",
        }


def _load_dotenv():
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    if Path(".env").exists():
        load_dotenv(".env", override=False)


def load_settings(config_file: str | os.PathLike | None = None,
                  env: dict[str, str] | None = None) -> Settings:
    """Build validated Settings. Raises ConfigError listing every problem.

    Passing `env` explicitly (as tests do) skips .env and the ./config.toml lookup.
    """
    use_defaults = env is None
    if env is None:
        _load_dotenv()
        env = dict(os.environ)
    problems: list[str] = []

    def dec(value, name, *, allow_zero_as_off=True) -> Decimal | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        try:
            d = Decimal(str(value).strip())
        except InvalidOperation:
            problems.append(f"{name} must be a number (got {value!r})")
            return None
        if not d.is_finite() or d < 0:
            problems.append(f"{name} must be a non-negative number (got {value!r})")
            return None
        return None if (d == 0 and allow_zero_as_off) else d

    def flt(value, name, default, minimum=0.0) -> float:
        if value is None or (isinstance(value, str) and not value.strip()):
            return default
        try:
            f = float(str(value).strip())
        except ValueError:
            problems.append(f"{name} must be a number (got {value!r})")
            return default
        if f != f or f < minimum:
            problems.append(f"{name} must be >= {minimum:g} (got {value!r})")
            return default
        return f

    path = config_file or env.get("CONFIG_FILE") or None
    raw: dict = {}
    if not path and use_defaults and Path("config.toml").exists():
        path = "config.toml"
    if path:
        p = Path(path)
        if not p.exists():
            raise ConfigError([f"config file not found: {p}"])
        try:
            raw = tomllib.loads(p.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise ConfigError([f"{p} is not valid TOML: {e}"]) from e

    def pick(env_key: str, key: str):
        v = env.get(env_key, "")
        return v if v.strip() else raw.get(key)

    s = Settings(config_file=Path(path) if path else None)

    mode = str(pick("MODE", "mode") or "monitor").strip().lower()
    if mode not in MODES:
        problems.append(f"MODE must be one of: {', '.join(MODES)} (got {mode!r})")
        mode = "monitor"
    s.mode = mode
    s.data_dir = Path(pick("DATA_DIR", "data_dir") or "data")
    s.log_dir = Path(pick("LOG_DIR", "log_dir") or "logs")
    level = str(pick("LOG_LEVEL", "log_level") or "INFO").strip().upper()
    if level not in LOG_LEVELS:
        problems.append(f"LOG_LEVEL must be one of: {', '.join(LOG_LEVELS)}")
        level = "INFO"
    s.log_level = level
    s.log_json = str(pick("LOG_JSON", "log_json") or "true").strip().lower() \
        not in ("0", "false", "no", "off")

    if raw.get("discord_webhook_url"):
        problems.append("put the webhook in the DISCORD_WEBHOOK_URL environment variable "
                        "(.env), not in the config file")
    url = env.get("DISCORD_WEBHOOK_URL", "").strip()
    if url and not WEBHOOK_RE.match(url):
        problems.append("DISCORD_WEBHOOK_URL does not look like a Discord webhook URL "
                        "(https://discord.com/api/webhooks/<id>/<token>)")
        url = ""
    s.discord_webhook_url = url
    s.discord_username = str(raw.get("discord_username") or s.discord_username)[:80]
    if "discord" in s.discord_username.lower():
        problems.append("discord_username must not contain \"discord\" (Discord rejects "
                        "every message from such a webhook name)")
    mention = str(pick("DISCORD_MENTION_USER_ID", "discord_mention_user_id") or "").strip()
    if mention and not mention.isdigit():
        problems.append("DISCORD_MENTION_USER_ID must be a numeric Discord user ID")
    elif mention:
        s.discord_mention = f"<@{mention}>"
    s.alert_cooldown_seconds = flt(raw.get("alert_cooldown_seconds"),
                                   "alert_cooldown_seconds", 900.0)

    s.poll_interval_seconds = flt(pick("POLL_INTERVAL_SECONDS", "poll_interval_seconds"),
                                  "POLL_INTERVAL_SECONDS", 30.0)
    if s.poll_interval_seconds < MIN_POLL_INTERVAL:
        problems.append(f"POLL_INTERVAL_SECONDS must be at least {MIN_POLL_INTERVAL:g}")
    s.request_timeout_seconds = flt(raw.get("request_timeout_seconds"),
                                    "request_timeout_seconds", 20.0, minimum=1.0)
    s.max_claim_price = dec(pick("MAX_CLAIM_PRICE", "max_claim_price"), "MAX_CLAIM_PRICE")
    s.max_daily_spend = dec(pick("MAX_DAILY_SPEND", "max_daily_spend"), "MAX_DAILY_SPEND")
    s.claim_hold_timeout_minutes = flt(
        pick("CLAIM_HOLD_TIMEOUT_MINUTES", "claim_hold_timeout_minutes"),
        "CLAIM_HOLD_TIMEOUT_MINUTES", 30.0)
    s.claim_status_interval_seconds = flt(raw.get("claim_status_interval_seconds"),
                                          "claim_status_interval_seconds", 30.0,
                                          minimum=MIN_POLL_INTERVAL)
    s.act_on_first_sight = str(pick("ACT_ON_FIRST_SIGHT", "act_on_first_sight") or "false"
                               ).strip().lower() in ("1", "true", "yes", "on")

    def limit(value, name, is_max: bool) -> Decimal | None:
        """Per-item price limit. 0 is a real number here, not "off": a max_price of 0
        would never claim, so it is refused instead of silently meaning no limit."""
        d = dec(value, name, allow_zero_as_off=False)
        if is_max and d is not None and d == 0:
            problems.append(f"{name} is 0, which never matches; remove it for no limit")
            return None
        return d

    seen_keys: set[str] = set()
    watch_raw = raw.get("watch", [])
    if not isinstance(watch_raw, list):
        problems.append("[[watch]] entries must be an array of tables")
        watch_raw = []
    for i, w in enumerate(watch_raw):
        if not isinstance(w, dict) or not str(w.get("item") or "").strip():
            problems.append(f"watch entry #{i + 1} needs an item name")
            continue
        item = str(w["item"]).strip()
        side = str(w.get("side") or "withdraw").strip().lower()
        if side not in SIDES:
            problems.append(f"watch entry {item!r}: side must be one of {', '.join(SIDES)}")
            continue
        key = norm_name(item)
        if key in seen_keys:
            problems.append(f"watch entry {item!r} is listed twice")
            continue
        seen_keys.add(key)
        s.watch.append(WatchEntry(item, side,
                                  limit(w.get("max_price"), f"max_price for {item!r}", True),
                                  limit(w.get("min_price"), f"min_price for {item!r}", False),
                                  key))
    # Quick env-only watchlist, handy on Railway:
    #   WATCH="Harvester<=330, Chroma Seer@deposit>=30, Icepiercer@both<=50>=10"
    for part in [p.strip() for p in env.get("WATCH", "").split(",") if p.strip()]:
        m = WATCH_ENTRY_RE.match(part)
        if not m:
            problems.append(f"WATCH entry {part!r}: use Item, Item@side, Item<=max or "
                            "Item@deposit>=min")
            continue
        item = m["item"].strip()
        side = (m["side"] or "withdraw").lower()
        key = norm_name(item)
        if side not in SIDES:
            problems.append(f"WATCH entry {part!r}: side must be one of {', '.join(SIDES)}")
            continue
        max_p = min_p = None
        for op, val in re.findall(r"(<=|>=)\s*([^<>=\s]+)", m["limits"] or ""):
            if op == "<=":
                max_p = limit(val, f"WATCH max price for {item!r}", True)
            else:
                min_p = limit(val, f"WATCH min price for {item!r}", False)
        if key and key not in seen_keys:
            seen_keys.add(key)
            s.watch.append(WatchEntry(item, side, max_p, min_p, key=key))
    if not s.watch:
        problems.append("the watchlist is empty: add [[watch]] entries to config.toml "
                        "or set WATCH in .env")

    s.bloxswaps_secrets = {k: v for k, v in env.items()
                           if k.startswith("BLOXSWAPS_") and v.strip()}
    if s.mode == "claim":
        if not s.discord_webhook_url:
            problems.append("claim mode needs DISCORD_WEBHOOK_URL, otherwise nobody hears "
                            "about a claim that must be traded")
        if not s.bloxswaps_secrets.get("BLOXSWAPS_TOKEN"):
            problems.append("claim mode needs BLOXSWAPS_TOKEN in .env (see README: "
                            "\"Getting your token\")")
        # Claim mode spends or gives away real items: every entry needs a price limit.
        for w in s.watch:
            if (w.side in ("withdraw", "both") and w.max_price is None
                    and s.max_claim_price is None):
                problems.append(f"claim mode: {w.item!r} would withdraw at any price. Set "
                                "max_price, write it as Item<=N in WATCH, or set "
                                "MAX_CLAIM_PRICE")
            if w.side in ("deposit", "both") and w.min_price is None:
                problems.append(f"claim mode: {w.item!r} would deposit for any payout. Set "
                                "min_price, or write it as Item@deposit>=N in WATCH")

    if problems:
        raise ConfigError(problems)
    return s
