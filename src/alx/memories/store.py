"""SQLite persistence for memories selected semantically by the AL/X Core."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from alx.contracts import (
    MemoryCorrection,
    MemoryKind,
    MemoryProposal,
    MemoryRevision,
    MemorySnapshot,
)


SCHEMA_VERSION = 1


class MemoryStoreError(Exception):
    pass


class MemoryNotFound(MemoryStoreError):
    pass


class MemoryAlreadyExists(MemoryStoreError):
    pass


class MemoryRevisionConflict(MemoryStoreError):
    pass


class SupersededMemoryNotFound(MemoryStoreError):
    pass


class MemoryIdentityConflict(MemoryStoreError):
    pass


def _aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _encode_revision(revision: MemoryRevision) -> str:
    return json.dumps(
        {
            "revision": revision.revision,
            "content": revision.content,
            "source_references": list(revision.source_references),
            "recorded_at": revision.recorded_at.isoformat(),
            "reason": revision.reason,
            "meaning": revision.meaning,
        },
        separators=(",", ":"),
    )


def _decode_revision(value: str) -> MemoryRevision:
    data = json.loads(value)
    return MemoryRevision(
        revision=data["revision"],
        content=data["content"],
        source_references=tuple(data["source_references"]),
        recorded_at=datetime.fromisoformat(data["recorded_at"]),
        reason=data["reason"],
        meaning=data["meaning"],
    )


class SQLiteMemoryStore:
    """Validates and persists Core proposals without assessing significance."""

    def __init__(self, database_path: str | Path) -> None:
        self._connection = sqlite3.connect(str(database_path))
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SQLiteMemoryStore:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _migrate(self) -> None:
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise MemoryStoreError(f"memory database schema {version} is newer than supported schema {SCHEMA_VERSION}")
        with self._connection:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS memories (memory_id TEXT PRIMARY KEY, kind TEXT NOT NULL, person_id TEXT, supersedes_memory_id TEXT, retention_until TEXT NOT NULL)"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS memory_revisions (memory_id TEXT NOT NULL REFERENCES memories(memory_id) ON DELETE CASCADE, revision INTEGER NOT NULL, revision_json TEXT NOT NULL, PRIMARY KEY(memory_id, revision))"
            )
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def create(self, proposal: MemoryProposal, retention_until: datetime) -> MemorySnapshot:
        _aware(retention_until, "retention_until")
        if proposal.supersedes_memory_id is not None and not self._exists(proposal.supersedes_memory_id):
            raise SupersededMemoryNotFound(proposal.supersedes_memory_id)
        revision = MemoryRevision(
            1, proposal.content, proposal.source_references, proposal.formed_at,
            meaning=proposal.meaning,
        )
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO memories(memory_id, kind, person_id, supersedes_memory_id, retention_until) VALUES (?, ?, ?, ?, ?)",
                    (proposal.memory_id, proposal.kind.value, proposal.person_id, proposal.supersedes_memory_id, retention_until.isoformat()),
                )
                self._connection.execute(
                    "INSERT INTO memory_revisions(memory_id, revision, revision_json) VALUES (?, ?, ?)",
                    (proposal.memory_id, 1, _encode_revision(revision)),
                )
        except sqlite3.IntegrityError as error:
            raise MemoryAlreadyExists(proposal.memory_id) from error
        return self.load(proposal.memory_id)

    def remember(self, proposal: MemoryProposal, retention_until: datetime) -> MemorySnapshot:
        """Persist once, while making an identical Core retry harmless."""
        try:
            return self.create(proposal, retention_until)
        except MemoryAlreadyExists:
            existing = self.load(proposal.memory_id)
            initial = existing.revisions[0]
            if (
                existing.kind is proposal.kind
                and existing.person_id == proposal.person_id
                and existing.supersedes_memory_id == proposal.supersedes_memory_id
                and initial.content == proposal.content
                and initial.source_references == proposal.source_references
                and initial.recorded_at == proposal.formed_at
                and initial.meaning == proposal.meaning
                and existing.retention_until == retention_until
            ):
                return existing
            raise MemoryIdentityConflict(proposal.memory_id)

    def load(self, memory_id: str) -> MemorySnapshot:
        row = self._connection.execute(
            "SELECT kind, person_id, supersedes_memory_id, retention_until FROM memories WHERE memory_id = ?",
            (memory_id,),
        ).fetchone()
        if row is None:
            raise MemoryNotFound(memory_id)
        revisions = tuple(
            _decode_revision(item[0])
            for item in self._connection.execute(
                "SELECT revision_json FROM memory_revisions WHERE memory_id = ? ORDER BY revision",
                (memory_id,),
            )
        )
        return MemorySnapshot(memory_id, MemoryKind(row[0]), row[1], row[2], revisions, datetime.fromisoformat(row[3]))

    def list_memories(self, kind: MemoryKind, *, person_id: str | None = None) -> tuple[MemorySnapshot, ...]:
        if kind is MemoryKind.RELATIONSHIP and person_id is None:
            raise ValueError("relationship memories must be inspected for one person_id")
        if kind is not MemoryKind.RELATIONSHIP and person_id is not None:
            raise ValueError("person_id filtering is only valid for relationship memory")
        if person_id is None:
            rows = self._connection.execute(
                "SELECT memory_id FROM memories WHERE kind = ? ORDER BY memory_id", (kind.value,)
            )
        else:
            rows = self._connection.execute(
                "SELECT memory_id FROM memories WHERE kind = ? AND person_id = ? ORDER BY memory_id",
                (kind.value, person_id),
            )
        return tuple(self.load(row[0]) for row in rows)

    def correct(self, memory_id: str, correction: MemoryCorrection, expected_revision: int) -> MemorySnapshot:
        current = self.load(memory_id)
        if current.revision != expected_revision:
            raise MemoryRevisionConflict(memory_id)
        if current.kind is MemoryKind.AUTOBIOGRAPHICAL and correction.meaning is None:
            raise ValueError("an autobiographical correction must preserve or revise its meaning")
        if current.kind is not MemoryKind.AUTOBIOGRAPHICAL and correction.meaning is not None:
            raise ValueError("meaning is reserved for autobiographical memory")
        revision = MemoryRevision(
            expected_revision + 1,
            correction.content,
            correction.source_references,
            correction.corrected_at,
            correction.reason,
            correction.meaning,
        )
        try:
            with self._connection:
                cursor = self._connection.execute(
                    "UPDATE memories SET retention_until = retention_until WHERE memory_id = ? AND (SELECT MAX(revision) FROM memory_revisions WHERE memory_id = ?) = ?",
                    (memory_id, memory_id, expected_revision),
                )
                if cursor.rowcount != 1:
                    raise MemoryRevisionConflict(memory_id)
                self._connection.execute(
                    "INSERT INTO memory_revisions(memory_id, revision, revision_json) VALUES (?, ?, ?)",
                    (memory_id, revision.revision, _encode_revision(revision)),
                )
        except sqlite3.IntegrityError as error:
            raise MemoryRevisionConflict(memory_id) from error
        return self.load(memory_id)

    def delete(self, memory_id: str, expected_revision: int) -> None:
        current = self.load(memory_id)
        if current.revision != expected_revision:
            raise MemoryRevisionConflict(memory_id)
        with self._connection:
            cursor = self._connection.execute("DELETE FROM memories WHERE memory_id = ?", (memory_id,))
            if cursor.rowcount != 1:
                raise MemoryNotFound(memory_id)

    def purge_expired(self, now: datetime) -> tuple[str, ...]:
        _aware(now, "now")
        identifiers = tuple(
            row[0] for row in self._connection.execute(
                "SELECT memory_id FROM memories WHERE retention_until <= ? ORDER BY memory_id", (now.isoformat(),)
            )
        )
        with self._connection:
            self._connection.executemany("DELETE FROM memories WHERE memory_id = ?", ((item,) for item in identifiers))
        return identifiers

    def _exists(self, memory_id: str) -> bool:
        return self._connection.execute("SELECT 1 FROM memories WHERE memory_id = ?", (memory_id,)).fetchone() is not None
