"""Where the watchlist comes from.

Without a Discord bot, it is the WATCH variable / config.toml, fixed at startup.
With DISCORD_BOT_TOKEN set, the list lives in the database (on the DATA_DIR volume)
and Discord commands change it. On the first start with an empty database the
WATCH / config.toml entries are copied in once; after that the database is the
list, so editing WATCH no longer changes anything (use /watch add and remove).
"""
from __future__ import annotations

from .config import Settings, WatchEntry
from .store import Store

SEEDED_KEY = "watch_seeded"
PAUSED_KEY = "paused"


class WatchList:
    def __init__(self, settings: Settings, store: Store):
        self.s = settings
        self.store = store
        self.managed = bool(settings.discord_bot_token)
        if self.managed and self.store.get_kv(SEEDED_KEY) is None:
            for w in settings.watch:
                self.store.put_watch(w)
            self.store.set_kv(SEEDED_KEY, "1")

    def entries(self) -> list[WatchEntry]:
        """Read once per sweep: a change from Discord applies on the next poll."""
        return self.store.watch_entries() if self.managed else list(self.s.watch)

    @staticmethod
    def find(entries: list[WatchEntry], name: str, slug: str, side: str) -> WatchEntry | None:
        return next((w for w in entries if w.matches(name, slug, side)), None)

    # ---- pause from Discord (separate from the claim hold)

    @property
    def paused(self) -> bool:
        return self.store.get_kv(PAUSED_KEY) == "1"

    def set_paused(self, value: bool):
        if value:
            self.store.set_kv(PAUSED_KEY, "1")
        else:
            self.store.delete_kv(PAUSED_KEY)
