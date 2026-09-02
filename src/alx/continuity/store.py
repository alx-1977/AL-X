"""Durable storage for AL/X's own future cognition requests.

Storage and nothing else. This module records a time, a note and an identity;
it never reads the note, never orders by anything but time, and never decides
whether a request should be honoured. Phase 5 asks it what is due; deciding
what to think about when a request matures is the Core's, always.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alx.contracts.continuity import (
    DuplicateFutureCognition,
    FutureCognitionNotFound,
    FutureCognitionRequest,
    FutureCognitionStatus,
)
from alx.contracts.provenance import provenance_from_storage, provenance_to_storage

SCHEMA_VERSION = 1


class SQLiteContinuityStore:
    """The one durable home for future cognition requests."""

    def __init__(self, database_path: str | Path, clock: Any = None) -> None:
        self._now = clock or (lambda: datetime.now(UTC))
        self._connection = sqlite3.connect(
            str(database_path), check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._migrate()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "SQLiteContinuityStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _migrate(self) -> None:
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS future_cognition (
                    request_id TEXT PRIMARY KEY,
                    not_before TEXT NOT NULL,
                    note TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    refs TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    content_origins TEXT,
                    content_recorded_at TEXT,
                    content_expires_at TEXT,
                    mail_references TEXT
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS future_cognition_due "
                "ON future_cognition(status, not_before)"
            )
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _row_to_request(row: sqlite3.Row) -> FutureCognitionRequest:
        return FutureCognitionRequest(
            request_id=row["request_id"],
            not_before=datetime.fromisoformat(row["not_before"]),
            # Returned exactly as written. No trimming, no normalising, no
            # decoding: a note that came back altered would be a different
            # message than the one she left herself.
            note=row["note"],
            requested_at=datetime.fromisoformat(row["requested_at"]),
            references=tuple(item for item in row["refs"].split("\x1f") if item),
            status=FutureCognitionStatus(row["status"]),
            provenance=provenance_from_storage(
                row["content_origins"],
                row["content_recorded_at"],
                row["content_expires_at"],
                row["mail_references"],
            ),
        )

    def create(self, request: FutureCognitionRequest) -> FutureCognitionRequest:
        """Store one request, or refuse a repeated identity."""
        stored = provenance_to_storage(request.provenance)
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO future_cognition(request_id, not_before, note, "
                    "requested_at, refs, status, content_origins, "
                    "content_recorded_at, content_expires_at, mail_references) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        request.request_id,
                        request.not_before.isoformat(),
                        request.note,
                        request.requested_at.isoformat(),
                        "\x1f".join(request.references),
                        request.status.value,
                        *stored,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise DuplicateFutureCognition(request.request_id) from error
        return self.load(request.request_id)

    def load(self, request_id: str) -> FutureCognitionRequest:
        row = self._connection.execute(
            "SELECT * FROM future_cognition WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is None:
            raise FutureCognitionNotFound(request_id)
        return self._row_to_request(row)

    def withdraw(self, request_id: str) -> FutureCognitionRequest:
        """Withdraw one exact request. Never more than the one named."""
        with self._connection:
            changed = self._connection.execute(
                "UPDATE future_cognition SET status = ? WHERE request_id = ? "
                "AND status = ?",
                (
                    FutureCognitionStatus.WITHDRAWN.value,
                    request_id,
                    FutureCognitionStatus.PENDING.value,
                ),
            ).rowcount
        if not changed:
            raise FutureCognitionNotFound(request_id)
        return self.load(request_id)

    def pending(self) -> tuple[FutureCognitionRequest, ...]:
        """Every request still waiting, soonest first.

        Ordered by time alone. There is nothing else to order by, and that is
        the point: no request outranks another.
        """
        rows = self._connection.execute(
            "SELECT * FROM future_cognition WHERE status = ? ORDER BY not_before",
            (FutureCognitionStatus.PENDING.value,),
        ).fetchall()
        return tuple(self._row_to_request(row) for row in rows)

    def due(self, now: datetime) -> tuple[FutureCognitionRequest, ...]:
        """Pending requests whose time has come. Phase 5 consumes these."""
        rows = self._connection.execute(
            "SELECT * FROM future_cognition WHERE status = ? AND not_before <= ? "
            "ORDER BY not_before",
            (FutureCognitionStatus.PENDING.value, now.isoformat()),
        ).fetchall()
        return tuple(self._row_to_request(row) for row in rows)

    def mark_honoured(self, request_id: str) -> FutureCognitionRequest:
        with self._connection:
            changed = self._connection.execute(
                "UPDATE future_cognition SET status = ? WHERE request_id = ? "
                "AND status = ?",
                (
                    FutureCognitionStatus.HONOURED.value,
                    request_id,
                    FutureCognitionStatus.PENDING.value,
                ),
            ).rowcount
        if not changed:
            raise FutureCognitionNotFound(request_id)
        return self.load(request_id)
