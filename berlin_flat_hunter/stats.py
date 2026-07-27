"""StatsLogger — SQLite-backed statistics about unique apartment notices"""
import sqlite3
import threading
import time

from flathunter.abstract_processor import Processor
from flathunter.logging import logger

# Free-text content columns carried per notice, in a stable order. Used to build
# the INSERT column list and to drive the re-sight backfill. ``price`` stays the
# legacy single value (what the notifier shows); ``price_cold``/``price_warm``
# hold the Kaltmiete/Warmmiete split for crawlers that expose it (Gewobag).
_CONTENT_COLS = (
    "url", "title", "address", "rooms", "size",
    "price", "price_cold", "price_warm",
)

# Composite primary key (id, crawler) — different crawlers mint independent IDs
# and a numeric collision across two sites must NOT silently drop a notice.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS notices (
    id INTEGER NOT NULL,
    crawler TEXT NOT NULL,
    url TEXT,
    title TEXT,
    address TEXT,
    rooms TEXT,
    size TEXT,
    price TEXT,
    price_cold TEXT,
    price_warm TEXT,
    first_seen_ts REAL NOT NULL,
    last_seen_ts REAL,
    PRIMARY KEY (id, crawler)
);
CREATE INDEX IF NOT EXISTS idx_notices_crawler ON notices(crawler);
CREATE INDEX IF NOT EXISTS idx_notices_first_seen_ts ON notices(first_seen_ts);
"""

# Columns added after the original schema shipped. Applied idempotently on open
# so existing stats DBs gain them without a manual migration. ``ADD COLUMN`` is
# a metadata-only op in SQLite (existing rows read NULL for the new column).
_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("price_cold", "TEXT"),
    ("price_warm", "TEXT"),
    ("last_seen_ts", "REAL"),
)


class StatsLogger:
    """Logs each unique expose (by ID) to a SQLite DB with timestamps.

    First sight of an ``(id, crawler)`` is inserted. On re-sight the row is not
    duplicated; instead any field that is still empty is backfilled from the
    fresh expose (public-housing listings — Gewobag especially — often appear
    first as "Auf Anfrage" teasers with no size/rooms/address and only fill in
    the concrete data days later), and ``last_seen_ts`` is bumped so listing
    longevity can be derived. Already-populated fields are never overwritten, so
    ``first_seen_ts`` semantics hold for everything captured at first sight.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        # WAL + NORMAL gives faster commits and concurrent readers without
        # losing crash safety. _lock serializes the cross-thread writer side.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()
        self._lock = threading.Lock()

    def _migrate(self) -> None:
        """Add columns introduced after the initial schema, if missing."""
        have = {row[1] for row in self._conn.execute("PRAGMA table_info(notices)")}
        for name, coltype in _MIGRATIONS:
            if name not in have:
                self._conn.execute(f"ALTER TABLE notices ADD COLUMN {name} {coltype}")

    def log(self, expose: dict) -> bool:
        """Record the expose. Returns True only if newly inserted (first sight).

        A re-sight returns False but may backfill previously-empty fields and
        always refreshes ``last_seen_ts``.
        """
        exp_id = expose.get("id")
        if exp_id is None:
            return False
        crawler = expose.get("crawler", "") or ""
        vals = {col: (expose.get(col) or "") for col in _CONTENT_COLS}
        now = time.time()
        with self._lock:
            cols = ("id", "crawler", *_CONTENT_COLS, "first_seen_ts", "last_seen_ts")
            placeholders = ",".join("?" * len(cols))
            params = (exp_id, crawler, *(vals[c] for c in _CONTENT_COLS), now, now)
            cur = self._conn.execute(
                f"INSERT OR IGNORE INTO notices ({','.join(cols)}) VALUES ({placeholders})",
                params,
            )
            if cur.rowcount > 0:
                self._conn.commit()
                return True
            self._backfill(exp_id, crawler, vals, now)
            self._conn.commit()
            return False

    def _backfill(self, exp_id, crawler: str, vals: dict, now: float) -> None:
        """Fill empty columns of an existing row from ``vals``; bump last_seen_ts.

        Only columns that are currently empty/NULL and have a non-empty fresh
        value are touched — first-seen values are preserved.
        """
        row = self._conn.execute(
            f"SELECT {','.join(_CONTENT_COLS)} FROM notices WHERE id=? AND crawler=?",
            (exp_id, crawler),
        ).fetchone()
        if row is None:
            return
        existing = dict(zip(_CONTENT_COLS, row))
        fills = {
            col: vals[col]
            for col in _CONTENT_COLS
            if not (existing[col] or "").strip() and vals[col].strip()
        }
        set_parts = ["last_seen_ts=?"]
        params: list = [now]
        for col, value in fills.items():
            set_parts.append(f"{col}=?")
            params.append(value)
        params.extend([exp_id, crawler])
        self._conn.execute(
            f"UPDATE notices SET {', '.join(set_parts)} WHERE id=? AND crawler=?",
            params,
        )
        if fills:
            logger.debug("Stats: backfilled %s for id=%s (%s)",
                         ",".join(fills), exp_id, crawler)

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
        cols = ("id", "url", "title", "address", "rooms", "size", "price",
                "price_cold", "price_warm", "crawler", "first_seen_ts", "last_seen_ts")
        rows = self._conn.execute(
            f"SELECT {','.join(cols)} FROM notices ORDER BY first_seen_ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(zip(cols, row)) for row in rows]

    def prune(self, days: int) -> int:
        """Delete notices not seen within ``days`` days. No-op when ``days`` <= 0.
        Returns the number of rows removed."""
        if not days or days <= 0:
            return 0
        cutoff = time.time() - days * 86400
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM notices WHERE last_seen_ts IS NOT NULL AND last_seen_ts < ?",
                (cutoff,),
            )
            self._conn.commit()
            return cur.rowcount

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
