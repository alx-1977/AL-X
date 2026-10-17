"""Retry copies of delivered replies into Friedl's conversation."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

MESSAGE_RETENTION = timedelta(days=30)


def _deadline(at: datetime, source_expires_at: datetime | None) -> datetime:
    limit = at + MESSAGE_RETENTION
    return limit if source_expires_at is None else min(limit, source_expires_at)


class SQLitePendingRelays:
    def __init__(self, path: str | Path) -> None:
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._connection:
            # One-time upgrade: discard old queued wording without reading it.
            self._connection.execute("DROP TABLE IF EXISTS pending_messages")
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS pending_relays (id INTEGER PRIMARY KEY "
                "AUTOINCREMENT, source_conversation_id TEXT NOT NULL, source_turn_id TEXT "
                "NOT NULL, target_conversation_id TEXT NOT NULL, expires_at TEXT NOT NULL)"
            )

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

    def close(self) -> None:
        self._connection.close()
