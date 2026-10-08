"""Replies AL/X made for Friedl that still need to reach him (D-044).

Two kinds of waiting work, both durable across restarts:

- messages: replies made while nobody was connected, handed in order to his
  next session;
- relays: copies of a delivered reply into his conversation that failed, so
  his thread can still be given what he heard.

Their text can come from mail, so each expires at the earlier of thirty days
(D-013) and the deadline of the reply it came from, shown to him or not.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

MESSAGE_RETENTION = timedelta(days=30)


def _deadline(at: datetime, source_expires_at: datetime | None) -> datetime:
    limit = at + MESSAGE_RETENTION
    return limit if source_expires_at is None else min(limit, source_expires_at)


class SQLitePendingMessages:
    def __init__(self, path: str | Path) -> None:
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._connection:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS pending_messages (id INTEGER PRIMARY KEY "
                "AUTOINCREMENT, source_conversation_id TEXT NOT NULL, source_turn_id TEXT "
                "NOT NULL, text TEXT NOT NULL, queued_at TEXT NOT NULL, expires_at TEXT NOT NULL)"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS pending_relays (id INTEGER PRIMARY KEY "
                "AUTOINCREMENT, source_conversation_id TEXT NOT NULL, source_turn_id TEXT "
                "NOT NULL, target_conversation_id TEXT NOT NULL, expires_at TEXT NOT NULL)"
            )

    def add(self, source_conversation_id: str, source_turn_id: str, text: str,
            at: datetime, source_expires_at: datetime | None = None) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO pending_messages (source_conversation_id, source_turn_id, text, "
                "queued_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (source_conversation_id, source_turn_id, text, at.isoformat(),
                 _deadline(at, source_expires_at).isoformat()),
            )

    def take_all(self, now: datetime) -> tuple[tuple[str, str, str, datetime], ...]:
        """Every unexpired message, oldest first, as (conversation, turn, text, expires).

        Its deadline travels with it, so a message handed back to waiting keeps
        the deadline it already had rather than starting a new one.

        Removed in the same step. The caller hands them to a delivery queue
        without yielding in between, so nothing is taken without being given.
        """
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM pending_messages WHERE expires_at < ?", (now.isoformat(),))
            rows = self._connection.execute(
                "SELECT id, source_conversation_id, source_turn_id, text, expires_at "
                "FROM pending_messages ORDER BY id"
            ).fetchall()
            if rows:
                self._connection.execute(
                    "DELETE FROM pending_messages WHERE id <= ?", (rows[-1][0],))
        return tuple((row[1], row[2], row[3], datetime.fromisoformat(row[4])) for row in rows)

    def add_relay(self, source_conversation_id: str, source_turn_id: str,
                  target_conversation_id: str, at: datetime,
                  source_expires_at: datetime | None = None) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO pending_relays (source_conversation_id, source_turn_id, "
                "target_conversation_id, expires_at) VALUES (?, ?, ?, ?)",
                (source_conversation_id, source_turn_id, target_conversation_id,
                 _deadline(at, source_expires_at).isoformat()),
            )

    def relays(self, now: datetime) -> tuple[tuple[int, str, str, str], ...]:
        """Unexpired relays still to make, oldest first, as (id, source, turn, target)."""
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM pending_relays WHERE expires_at < ?", (now.isoformat(),))
            rows = self._connection.execute(
                "SELECT id, source_conversation_id, source_turn_id, target_conversation_id "
                "FROM pending_relays ORDER BY id"
            ).fetchall()
        return tuple((row[0], row[1], row[2], row[3]) for row in rows)

    def relay_done(self, relay_id: int) -> None:
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM pending_relays WHERE id = ?", (relay_id,))

    def count(self) -> int:
        with self._lock:
            return self._connection.execute(
                "SELECT COUNT(*) FROM pending_messages").fetchone()[0]

    def close(self) -> None:
        self._connection.close()
