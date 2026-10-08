"""Messages AL/X wanted Friedl to hear while nobody was connected (D-044).

Kept in order and handed to the next session that opens, then removed. Their
text can come from mail, so they expire with mail-derived content (30 days,
D-013) whether or not they were ever shown.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

MESSAGE_RETENTION = timedelta(days=30)


class SQLitePendingMessages:
    def __init__(self, path: str | Path) -> None:
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._connection:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS pending_messages (id INTEGER PRIMARY KEY "
                "AUTOINCREMENT, source_conversation_id TEXT NOT NULL, text TEXT NOT NULL, "
                "queued_at TEXT NOT NULL, expires_at TEXT NOT NULL)"
            )

    def add(self, source_conversation_id: str, text: str, at: datetime) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO pending_messages (source_conversation_id, text, queued_at, "
                "expires_at) VALUES (?, ?, ?, ?)",
                (source_conversation_id, text, at.isoformat(),
                 (at + MESSAGE_RETENTION).isoformat()),
            )

    def take_all(self, now: datetime) -> tuple[tuple[str, str], ...]:
        """Every unexpired message, oldest first, removed as it is taken."""
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM pending_messages WHERE expires_at < ?", (now.isoformat(),))
            rows = self._connection.execute(
                "SELECT source_conversation_id, text FROM pending_messages ORDER BY id"
            ).fetchall()
            self._connection.execute("DELETE FROM pending_messages")
        return tuple((row[0], row[1]) for row in rows)

    def count(self) -> int:
        with self._lock:
            return self._connection.execute(
                "SELECT COUNT(*) FROM pending_messages").fetchone()[0]

    def close(self) -> None:
        self._connection.close()
