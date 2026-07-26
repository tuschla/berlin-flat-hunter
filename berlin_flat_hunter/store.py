"""Per-profile SQLite Store for the email-alias + application state.

Backs the addy.io alias cache, the send/apply dedup log, and the IMAP-side
double-opt-in / reply-notification dedup tables with one sqlite file per profile.
Raw sqlite3 (no ORM), WAL mode for concurrent reads, idempotent CREATE TABLE.

``user_id`` is the profile name (e.g. "single", "wg"); ``listing_key`` is a
caller-supplied string (e.g. f"{crawler}:{id}").
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
-- Cached addy.io aliases so we don't mint a fresh one every cycle. scope_key is
-- the granularity bucket: a source name, a listing key, or the literal "fixed".
CREATE TABLE IF NOT EXISTS aliases (
    user_id     TEXT NOT NULL,
    scope_key   TEXT NOT NULL,
    alias_email TEXT NOT NULL,
    alias_id    TEXT NOT NULL DEFAULT '',
    ts          INTEGER NOT NULL,
    PRIMARY KEY (user_id, scope_key)
);

CREATE TABLE IF NOT EXISTS sends (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    listing_key TEXT NOT NULL,
    ts          INTEGER NOT NULL,
    mode        TEXT NOT NULL,           -- dry_run | live
    channel     TEXT NOT NULL,           -- howoge-form | gewobag-form | wbm-form
    ok          INTEGER NOT NULL,
    message     TEXT NOT NULL DEFAULT '',
    recipient   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sends_user_listing ON sends(user_id, listing_key);

-- Genossenschaft double-opt-in links we've already confirmed successfully.
-- Keyed by URL because DOI links are one-time tokens; failed attempts are not
-- recorded so transient HTTP/IMAP problems retry on the next cycle.
CREATE TABLE IF NOT EXISTS imap_confirmations (
    user_id TEXT NOT NULL,
    url     TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    ts      INTEGER NOT NULL,
    PRIMARY KEY (user_id, url)
);

-- Landlord email replies we've already pushed to Telegram (dedup by Message-ID),
-- so the same reply isn't re-announced on every IMAP scan.
CREATE TABLE IF NOT EXISTS email_notifications (
    user_id    TEXT NOT NULL,
    message_id TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    PRIMARY KEY (user_id, message_id)
);
"""


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ── email aliases (addy.io) ────────────────────────────────────────
    def get_alias(self, user_id: str, scope_key: str) -> str | None:
        row = self._conn.execute(
            "SELECT alias_email FROM aliases WHERE user_id = ? AND scope_key = ?",
            (user_id, scope_key),
        ).fetchone()
        return row["alias_email"] if row else None

    def save_alias(self, user_id: str, scope_key: str, alias_email: str, alias_id: str = "") -> None:
        self._conn.execute(
            "INSERT INTO aliases(user_id, scope_key, alias_email, alias_id, ts)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id, scope_key) DO UPDATE SET"
            "   alias_email = excluded.alias_email,"
            "   alias_id = excluded.alias_id,"
            "   ts = excluded.ts",
            (user_id, scope_key, alias_email, alias_id, int(time.time())),
        )
        self._conn.commit()

    # ── sends ──────────────────────────────────────────────────────────
    def record_send(
        self, user_id: str, listing_key: str, *, mode: str, channel: str,
        ok: bool, message: str = "", recipient: str = "",
    ) -> None:
        self._conn.execute(
            "INSERT INTO sends(user_id, listing_key, ts, mode, channel, ok, message, recipient)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, listing_key, int(time.time()), mode, channel,
             1 if ok else 0, message, recipient),
        )
        self._conn.commit()

    def has_live_send(self, user_id: str, listing_key: str, recipient: str | None = None) -> bool:
        """A successful live send for this listing (optionally to a specific
        recipient, so each chosen email counts as its own application)."""
        if recipient is not None:
            row = self._conn.execute(
                "SELECT 1 FROM sends WHERE user_id = ? AND listing_key = ?"
                " AND mode = 'live' AND ok = 1 AND recipient = ? LIMIT 1",
                (user_id, listing_key, recipient),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT 1 FROM sends WHERE user_id = ? AND listing_key = ?"
                " AND mode = 'live' AND ok = 1 LIMIT 1",
                (user_id, listing_key),
            ).fetchone()
        return row is not None

    def has_send(self, user_id: str, listing_key: str, mode: str | None = None,
                 recipient: str | None = None) -> bool:
        """Any send attempt recorded for this listing (optionally mode/recipient)."""
        sql = "SELECT 1 FROM sends WHERE user_id = ? AND listing_key = ?"
        params: list[Any] = [user_id, listing_key]
        if mode:
            sql += " AND mode = ?"
            params.append(mode)
        if recipient is not None:
            sql += " AND recipient = ?"
            params.append(recipient)
        sql += " LIMIT 1"
        return self._conn.execute(sql, params).fetchone() is not None

    # ── IMAP double-opt-in confirmations ───────────────────────────────
    def was_imap_confirmed(self, user_id: str, url: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM imap_confirmations WHERE user_id = ? AND url = ? LIMIT 1",
            (user_id, url),
        ).fetchone()
        return row is not None

    def record_imap_confirmation(self, user_id: str, subject: str, url: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO imap_confirmations(user_id, url, subject, ts) VALUES (?, ?, ?, ?)",
            (user_id, url, subject, int(time.time())),
        )
        self._conn.commit()

    # ── email reply notifications ──────────────────────────────────────
    def was_email_notified(self, user_id: str, message_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM email_notifications WHERE user_id = ? AND message_id = ? LIMIT 1",
            (user_id, message_id),
        ).fetchone()
        return row is not None

    def record_email_notification(self, user_id: str, message_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO email_notifications(user_id, message_id, ts) VALUES (?, ?, ?)",
            (user_id, message_id, int(time.time())),
        )
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
