"""StatsLogger — SQLite-backed statistics about unique apartment notices"""
import sqlite3
import threading
import time

from flathunter.abstract_processor import Processor
from flathunter.logging import logger

# Composite primary key (id, crawler) — different crawlers mint independent IDs
# and a numeric collision across two sites must NOT silently drop a notice.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS notices (
    id INTEGER NOT NULL,
    crawler TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT,
    address TEXT,
    rooms TEXT,
    size TEXT,
    price TEXT,
    first_seen_ts REAL NOT NULL,
    PRIMARY KEY (id, crawler)
);
CREATE INDEX IF NOT EXISTS idx_notices_crawler ON notices(crawler);
CREATE INDEX IF NOT EXISTS idx_notices_first_seen_ts ON notices(first_seen_ts);
"""


class StatsLogger:
    """Logs each unique expose (by ID) to a SQLite DB with timestamps.

    Insert uses INSERT OR IGNORE so only first occurrence is stored.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        # WAL + NORMAL gives faster commits and concurrent readers without
        # losing crash safety. _lock serializes the cross-thread writer side.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

    def log(self, expose: dict) -> bool:
        """Record the expose if not seen before. Returns True if newly logged."""
        exp_id = expose.get("id")
        if exp_id is None:
            return False
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO notices "
                "(id, crawler, url, title, address, rooms, size, price, first_seen_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    exp_id,
                    expose.get("crawler", ""),
                    expose.get("url", ""),
                    expose.get("title", ""),
                    expose.get("address", ""),
                    expose.get("rooms", ""),
                    expose.get("size", ""),
                    expose.get("price", ""),
                    time.time(),
                ),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def count_total(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM notices").fetchone()[0]

    def count_by_crawler(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT crawler, COUNT(*) FROM notices GROUP BY crawler"
        ).fetchall()
        return dict(rows)

    def count_since(self, since_ts: float) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM notices WHERE first_seen_ts >= ?", (since_ts,)
        ).fetchone()[0]

    def recent(self, limit: int = 50) -> list[dict]:
        cols = ("id", "url", "title", "address", "rooms", "size", "price", "crawler", "first_seen_ts")
        rows = self._conn.execute(
            f"SELECT {','.join(cols)} FROM notices ORDER BY first_seen_ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(zip(cols, row)) for row in rows]

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass


class StatsProcessor(Processor):
    """Processor that logs each expose to a StatsLogger (non-destructive)."""

    def __init__(self, logger_instance: StatsLogger):
        self.stats = logger_instance

    def process_expose(self, expose: dict) -> dict:
        try:
            if self.stats.log(expose):
                logger.debug("Stats: new notice logged id=%s", expose.get("id"))
        except Exception as exc:
            logger.warning("Stats log failed for %s: %s", expose.get("url"), exc)
        return expose
