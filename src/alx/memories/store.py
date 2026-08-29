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
    MemoryQuery,
    MemoryRevision,
    MemorySnapshot,
    MemorySourceMatch,
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


class InvalidMemorySupersession(MemoryStoreError):
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
        # Core turns execute on one serialized worker so blocking provider I/O
        # cannot stall the asyncio voice transport.
        self._connection = sqlite3.connect(str(database_path), check_same_thread=False)
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
        try:
            with self._connection:
                self._insert(proposal, retention_until)
        except sqlite3.IntegrityError as error:
            raise MemoryAlreadyExists(proposal.memory_id) from error
        return self.load(proposal.memory_id)

    def remember(self, proposal: MemoryProposal, retention_until: datetime) -> MemorySnapshot:
        """Persist once, while making an identical Core retry harmless."""
        return self.remember_many((proposal,), retention_until)[0]

    def remember_many(
        self,
        proposals: tuple[MemoryProposal, ...],
        retention_until: datetime,
    ) -> tuple[MemorySnapshot, ...]:
        """Persist one Core-selected batch atomically and idempotently."""
        _aware(retention_until, "retention_until")
        proposal_list = tuple(proposals)
        if not proposal_list:
            return ()
        memory_ids = [proposal.memory_id for proposal in proposal_list]
        if len(memory_ids) != len(set(memory_ids)):
            raise ValueError("a memory batch cannot repeat a memory identifier")
        with self._connection:
            for proposal in proposal_list:
                try:
                    self._insert(proposal, retention_until)
                except sqlite3.IntegrityError:
                    existing = self.load(proposal.memory_id)
                    if not self._matches_proposal(existing, proposal, retention_until):
                        raise MemoryIdentityConflict(proposal.memory_id)
        return tuple(self.load(proposal.memory_id) for proposal in proposal_list)

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

    def retrieve(self, query: MemoryQuery, as_of: datetime) -> tuple[MemorySnapshot, ...]:
        """Apply Core-selected metadata constraints without interpreting meaning."""
        _aware(as_of, "as_of")
        snapshots = tuple(
            self.load(row[0])
            for row in self._connection.execute("SELECT memory_id FROM memories ORDER BY memory_id")
        )
        live_snapshots = tuple(item for item in snapshots if item.retention_until > as_of)
        superseded_ids = {
            item.supersedes_memory_id
            for item in live_snapshots
            if item.supersedes_memory_id is not None
        }
        selected = []
        for item in live_snapshots:
            current = item.current
            formed_at = item.revisions[0].recorded_at
            if formed_at > as_of or current.recorded_at > as_of:
                continue
            sources = set(current.source_references)
            requested_sources = set(query.source_references)
            if query.kinds and item.kind not in query.kinds:
                continue
            if query.memory_ids and item.memory_id not in query.memory_ids:
                continue
            if (
                item.kind is MemoryKind.RELATIONSHIP
                and item.person_id != query.person_id
            ):
                continue
            if query.formed_after is not None and formed_at < query.formed_after:
                continue
            if query.formed_before is not None and formed_at > query.formed_before:
                continue
            if requested_sources:
                matches = requested_sources.intersection(sources)
                if query.source_match is MemorySourceMatch.ANY and not matches:
                    continue
                if query.source_match is MemorySourceMatch.ALL and not requested_sources.issubset(sources):
                    continue
            if not query.include_superseded and item.memory_id in superseded_ids:
                continue
            selected.append(item)
        return tuple(selected)

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
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM memories WHERE memory_id = ? AND (SELECT MAX(revision) FROM memory_revisions WHERE memory_id = ?) = ?",
                (memory_id, memory_id, expected_revision),
            )
            if cursor.rowcount != 1:
                if self._exists(memory_id):
                    raise MemoryRevisionConflict(memory_id)
                raise MemoryNotFound(memory_id)

    def purge_expired(self, now: datetime) -> tuple[str, ...]:
        _aware(now, "now")
        identifiers = tuple(
            row[0]
            for row in self._connection.execute(
                "SELECT memory_id, retention_until FROM memories ORDER BY memory_id"
            )
            if datetime.fromisoformat(row[1]) <= now
        )
        with self._connection:
            self._connection.executemany("DELETE FROM memories WHERE memory_id = ?", ((item,) for item in identifiers))
        return identifiers

    def _exists(self, memory_id: str) -> bool:
        return self._connection.execute("SELECT 1 FROM memories WHERE memory_id = ?", (memory_id,)).fetchone() is not None

    def _insert(self, proposal: MemoryProposal, retention_until: datetime) -> None:
        if proposal.supersedes_memory_id is not None:
            if not self._exists(proposal.supersedes_memory_id):
                raise SupersededMemoryNotFound(proposal.supersedes_memory_id)
            previous = self.load(proposal.supersedes_memory_id)
            if previous.kind is not proposal.kind or previous.person_id != proposal.person_id:
                raise InvalidMemorySupersession(proposal.supersedes_memory_id)
        revision = MemoryRevision(
            1,
            proposal.content,
            proposal.source_references,
            proposal.formed_at,
            meaning=proposal.meaning,
        )
        self._connection.execute(
            "INSERT INTO memories(memory_id, kind, person_id, supersedes_memory_id, retention_until) VALUES (?, ?, ?, ?, ?)",
            (proposal.memory_id, proposal.kind.value, proposal.person_id, proposal.supersedes_memory_id, retention_until.isoformat()),
        )
        self._connection.execute(
            "INSERT INTO memory_revisions(memory_id, revision, revision_json) VALUES (?, ?, ?)",
            (proposal.memory_id, 1, _encode_revision(revision)),
        )

    @staticmethod
    def _matches_proposal(
        existing: MemorySnapshot,
        proposal: MemoryProposal,
        retention_until: datetime,
    ) -> bool:
        initial = existing.revisions[0]
        return (
            existing.kind is proposal.kind
            and existing.person_id == proposal.person_id
            and existing.supersedes_memory_id == proposal.supersedes_memory_id
            and initial.content == proposal.content
            and initial.source_references == proposal.source_references
            and initial.recorded_at == proposal.formed_at
            and initial.meaning == proposal.meaning
            and existing.retention_until == retention_until
        )
