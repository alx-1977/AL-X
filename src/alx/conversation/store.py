"""Independent durable storage for the one continuous AL/X conversation."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from alx.contracts import ConversationOrigin, ConversationSnapshot, ConversationTurn


SCHEMA_VERSION = 1


class ConversationStoreError(Exception):
    pass


class ConversationNotFound(ConversationStoreError):
    pass


class ConversationAlreadyExists(ConversationStoreError):
    pass


class ConversationRevisionConflict(ConversationStoreError):
    pass


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _turn_to_json(turn: ConversationTurn) -> str:
    return json.dumps(
        [
            turn.conversation_id,
            turn.turn_id,
            turn.origin.value,
            turn.content,
            turn.occurred_at.isoformat(),
            turn.person_id,
        ],
        separators=(",", ":"),
    )


def _turn_from_json(value: str) -> ConversationTurn:
    data = json.loads(value)
    return ConversationTurn(
        data[0],
        data[1],
        ConversationOrigin(data[2]),
        data[3],
        datetime.fromisoformat(data[4]),
        data[5],
    )


class SQLiteConversationStore:
    """Stores conversation independently; it never interprets or routes content."""

    def __init__(self, database_path: str | Path) -> None:
        # Core turns execute on one serialized worker so blocking provider I/O
        # cannot stall the asyncio voice transport.
        self._connection = sqlite3.connect(str(database_path), check_same_thread=False)
        self._connection.execute("PRAGMA foreign_keys = ON")
        with self._connection:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS conversations "
                "(conversation_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, retention_until TEXT NOT NULL)"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS conversation_turns "
                "(conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE, "
                "ordinal INTEGER NOT NULL, turn_id TEXT NOT NULL, turn_json TEXT NOT NULL, "
                "PRIMARY KEY(conversation_id, ordinal), UNIQUE(conversation_id, turn_id))"
            )
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self._connection.close()

    def create(self, conversation_id: str, retention_until: datetime) -> ConversationSnapshot:
        if not conversation_id.strip():
            raise ValueError("conversation_id must not be blank")
        _aware(retention_until, "retention_until")
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO conversations(conversation_id, revision, retention_until) VALUES (?, 1, ?)",
                    (conversation_id, retention_until.isoformat()),
                )
        except sqlite3.IntegrityError as error:
            raise ConversationAlreadyExists(conversation_id) from error
        return ConversationSnapshot(conversation_id, (), 1, retention_until)

    def load(self, conversation_id: str) -> ConversationSnapshot:
        row = self._connection.execute(
            "SELECT revision, retention_until FROM conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            raise ConversationNotFound(conversation_id)
        turns = tuple(
            _turn_from_json(item[0])
            for item in self._connection.execute(
                "SELECT turn_json FROM conversation_turns WHERE conversation_id = ? ORDER BY ordinal",
                (conversation_id,),
            )
        )
        return ConversationSnapshot(
            conversation_id,
            turns,
            row[0],
            datetime.fromisoformat(row[1]),
        )

    def append(
        self,
        turn: ConversationTurn,
        retention_until: datetime,
        expected_revision: int,
    ) -> ConversationSnapshot:
        _aware(retention_until, "retention_until")
        with self._connection:
            updated = self._connection.execute(
                "UPDATE conversations SET revision = revision + 1, retention_until = ? "
                "WHERE conversation_id = ? AND revision = ?",
                (retention_until.isoformat(), turn.conversation_id, expected_revision),
            ).rowcount
            if not updated:
                if self._connection.execute(
                    "SELECT 1 FROM conversations WHERE conversation_id = ?",
                    (turn.conversation_id,),
                ).fetchone():
                    raise ConversationRevisionConflict(turn.conversation_id)
                raise ConversationNotFound(turn.conversation_id)
            ordinal = self._connection.execute(
                "SELECT COUNT(*) FROM conversation_turns WHERE conversation_id = ?",
                (turn.conversation_id,),
            ).fetchone()[0]
            self._connection.execute(
                "INSERT INTO conversation_turns(conversation_id, ordinal, turn_id, turn_json) VALUES (?, ?, ?, ?)",
                (turn.conversation_id, ordinal, turn.turn_id, _turn_to_json(turn)),
            )
        return self.load(turn.conversation_id)

    def list_conversations(self) -> tuple[ConversationSnapshot, ...]:
        identifiers = self._connection.execute(
            "SELECT conversation_id FROM conversations ORDER BY conversation_id"
        ).fetchall()
        return tuple(self.load(item[0]) for item in identifiers)

    def delete(self, conversation_id: str, expected_revision: int) -> None:
        with self._connection:
            deleted = self._connection.execute(
                "DELETE FROM conversations WHERE conversation_id = ? AND revision = ?",
                (conversation_id, expected_revision),
            ).rowcount
            if not deleted:
                if self._connection.execute(
                    "SELECT 1 FROM conversations WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone():
                    raise ConversationRevisionConflict(conversation_id)
                raise ConversationNotFound(conversation_id)

    def purge_expired(self, at: datetime) -> tuple[str, ...]:
        _aware(at, "at")
        rows = self._connection.execute(
            "SELECT conversation_id, retention_until FROM conversations ORDER BY conversation_id"
        ).fetchall()
        identifiers = tuple(
            identifier
            for identifier, retention in rows
            if datetime.fromisoformat(retention) <= at
        )
        with self._connection:
            self._connection.executemany(
                "DELETE FROM conversations WHERE conversation_id = ?",
                ((identifier,) for identifier in identifiers),
            )
        return identifiers
