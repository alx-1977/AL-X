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
    CarriedThought,
    CarriedThoughtNotFound,
    CarriedThoughtStatus,
    DuplicateCarriedThought,
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
                    conversation_id TEXT NOT NULL DEFAULT '',
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
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS carried_thoughts (
                    thought_id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    formed_at TEXT NOT NULL,
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
                "CREATE INDEX IF NOT EXISTS carried_thoughts_open "
                "ON carried_thoughts(status, formed_at)"
            )
            existing = {
                row[1]
                for row in self._connection.execute(
                    "PRAGMA table_info(future_cognition)"
                ).fetchall()
            }
            if "conversation_id" not in existing:
                self._connection.execute(
                    "ALTER TABLE future_cognition "
                    "ADD COLUMN conversation_id TEXT NOT NULL DEFAULT ''"
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
            conversation_id=row["conversation_id"],
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
                    "requested_at, conversation_id, refs, status, "
                    "content_origins, content_recorded_at, content_expires_at, "
                    "mail_references) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        request.request_id,
                        request.not_before.isoformat(),
                        request.note,
                        request.requested_at.isoformat(),
                        request.conversation_id,
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

    # --- carried thoughts -------------------------------------------------
    #
    # The same store, because one durable home for continuity state is one
    # production path. The methods below move a thought between three states
    # AL/X chooses; none of them reads what she wrote.

    @staticmethod
    def _row_to_thought(row: sqlite3.Row) -> CarriedThought:
        return CarriedThought(
            thought_id=row["thought_id"],
            # Verbatim, like the note. A thought that came back altered would
            # not be the one she formed.
            content=row["content"],
            formed_at=datetime.fromisoformat(row["formed_at"]),
            references=tuple(item for item in row["refs"].split("\x1f") if item),
            status=CarriedThoughtStatus(row["status"]),
            provenance=provenance_from_storage(
                row["content_origins"],
                row["content_recorded_at"],
                row["content_expires_at"],
                row["mail_references"],
            ),
        )

    def record_thought(self, thought: CarriedThought) -> CarriedThought:
        stored = provenance_to_storage(thought.provenance)
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO carried_thoughts(thought_id, content, formed_at, "
                    "refs, status, content_origins, content_recorded_at, "
                    "content_expires_at, mail_references) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        thought.thought_id,
                        thought.content,
                        thought.formed_at.isoformat(),
                        "\x1f".join(thought.references),
                        thought.status.value,
                        *stored,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise DuplicateCarriedThought(thought.thought_id) from error
        return self.load_thought(thought.thought_id)

    def load_thought(self, thought_id: str) -> CarriedThought:
        row = self._connection.execute(
            "SELECT * FROM carried_thoughts WHERE thought_id = ?", (thought_id,)
        ).fetchone()
        if row is None:
            raise CarriedThoughtNotFound(thought_id)
        return self._row_to_thought(row)

    def _set_thought_status(
        self, thought_id: str, status: CarriedThoughtStatus
    ) -> CarriedThought:
        with self._connection:
            changed = self._connection.execute(
                "UPDATE carried_thoughts SET status = ? WHERE thought_id = ? "
                "AND status = ?",
                (status.value, thought_id, CarriedThoughtStatus.OPEN.value),
            ).rowcount
        if not changed:
            raise CarriedThoughtNotFound(thought_id)
        return self.load_thought(thought_id)

    def withdraw_thought(self, thought_id: str) -> CarriedThought:
        """AL/X no longer holds this. Only the one named."""
        return self._set_thought_status(
            thought_id, CarriedThoughtStatus.WITHDRAWN
        )

    def mark_thought_raised(self, thought_id: str) -> CarriedThought:
        """She brought it into conversation. Never inferred from content."""
        return self._set_thought_status(thought_id, CarriedThoughtStatus.RAISED)

    def open_thoughts(self, limit: int = 20) -> tuple[CarriedThought, ...]:
        """Thoughts she still holds, most recent first.

        Recency is the only ordering. It encodes no judgement about subject
        matter, which is exactly why it is the one used: any other ordering
        would be the runtime deciding which of her thoughts matters most.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        rows = self._connection.execute(
            "SELECT * FROM carried_thoughts WHERE status = ? "
            "ORDER BY formed_at DESC, rowid DESC LIMIT ?",
            (CarriedThoughtStatus.OPEN.value, limit),
        ).fetchall()
        return tuple(self._row_to_thought(row) for row in rows)
