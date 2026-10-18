"""Console + rotating file logs. Every formatted line, tracebacks included,
passes through `redact`, so a token in an exception never reaches stderr or disk."""
from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path

from .notify import redact


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


class RedactingFormatter(logging.Formatter):
    """Redacts the whole formatted line, including tracebacks and stack info."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = {"ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
             "level": record.levelname, "logger": record.name,
             "msg": redact(record.getMessage())}
        if record.exc_info:
            d["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(d, ensure_ascii=False)


def setup_logging(level: str = "INFO", log_dir: Path | None = None, json_file: bool = True):
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    console = logging.StreamHandler()
    console.setFormatter(RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                                            "%Y-%m-%d %H:%M:%S"))
    console.addFilter(RedactFilter())
    root.addHandler(console)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_dir / "mm2sniper.log",
                                                  maxBytes=10_000_000, backupCount=5,
                                                  encoding="utf-8")
        fh.setFormatter(JsonFormatter() if json_file else console.formatter)
        fh.addFilter(RedactFilter())
        root.addHandler(fh)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.client").setLevel(logging.ERROR)  # "voice not supported" noise
