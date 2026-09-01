"""Durable research storage that persists thinking without interpreting it.

This is the memory store's proven shape applied to research: revisions are
append-only rows keyed by (id, revision), provenance uses the same four columns
and the same encoders, and concurrent edits are caught by an expected revision.

The store validates shape, ownership and retention. It never decides what a
claim means, whether a doubt is warranted, or whether an enquiry was worth
having. Those are AL/X's judgements and stay in the Core.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from alx.contracts.notebook import (
    DeletionRecord,
    EntryKind,
    EntryProposal,
    EntryRevision,
    EntryRevisionProposal,
    EntrySnapshot,
    ResearchQuery,
    ThreadProposal,
    ThreadSnapshot,
    ThreadStatus,
)
from alx.contracts.provenance import (
    ContentProvenance,
    provenance_from_storage,
    provenance_to_storage,
)


SCHEMA_VERSION = 1

PROVENANCE_COLUMNS = (
    "content_origins",
    "content_recorded_at",
    "content_expires_at",
    "mail_references",
)


class ResearchStoreError(Exception):
    """A research storage failure."""


class ThreadNotFound(ResearchStoreError):
    pass


class EntryNotFound(ResearchStoreError):
    pass


class ThreadAlreadyExists(ResearchStoreError):
    pass


class EntryAlreadyExists(ResearchStoreError):
    pass


class EntryRevisionConflict(ResearchStoreError):
    """Another writer revised this entry first; nothing was overwritten."""


class ArchivedThreadWrite(ResearchStoreError):
    """Archived research is put aside, so it does not take new thinking."""


def _aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


class SQLiteResearchStore:
    """Persist research threads and their revised entries."""

    def __init__(self, database_path: str | Path) -> None:
        self._connection = sqlite3.connect(
            str(database_path), check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        # Deleting a row ordinarily marks its page free and leaves the bytes on
        # disk, where the deleted research is still readable. Friedl's deletion
        # must actually remove the content, so overwrite freed pages with
        # zeroes. This is the difference between deleted and merely unlinked.
        self._connection.execute("PRAGMA secure_delete = ON")
        self._migrate()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "SQLiteResearchStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _migrate(self) -> None:
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise ResearchStoreError(
                f"research database schema {version} is newer than supported "
                f"schema {SCHEMA_VERSION}"
            )
        with self._connection:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS research_threads ("
                "thread_id TEXT PRIMARY KEY, question TEXT NOT NULL, "
                "interest TEXT NOT NULL, status TEXT NOT NULL, "
                "opened_at TEXT NOT NULL, retention_until TEXT NOT NULL, "
                "content_origins TEXT, content_recorded_at TEXT, "
                "content_expires_at TEXT, mail_references TEXT)"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS research_entries ("
                "entry_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL "
                "REFERENCES research_threads(thread_id) ON DELETE CASCADE, "
                "kind TEXT NOT NULL)"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS research_entry_revisions ("
                "entry_id TEXT NOT NULL REFERENCES research_entries(entry_id) "
                "ON DELETE CASCADE, revision INTEGER NOT NULL, "
                "content TEXT NOT NULL, reason TEXT, recorded_at TEXT NOT NULL, "
                "source_references TEXT NOT NULL, "
                "content_origins TEXT, content_recorded_at TEXT, "
                "content_expires_at TEXT, mail_references TEXT, "
                "PRIMARY KEY(entry_id, revision))"
            )
            # A deletion leaves an identifier and a time, and nothing else.
            # There is no content column here by design: an audit copy of
            # deleted research would be the secret second copy that deletion
            # is supposed to remove.
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS research_deletions ("
                "record_id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                "deleted_at TEXT NOT NULL)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS research_entries_thread "
                "ON research_entries(thread_id)"
            )
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # ---- threads --------------------------------------------------------

    def open_thread(
        self, proposal: ThreadProposal, retention_until: datetime
    ) -> ThreadSnapshot:
        _aware(retention_until, "retention_until")
        origins, recorded, expires, references = provenance_to_storage(
            proposal.provenance
        )
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO research_threads(thread_id, question, interest, "
                    "status, opened_at, retention_until, content_origins, "
                    "content_recorded_at, content_expires_at, mail_references) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        proposal.thread_id,
                        proposal.question,
                        proposal.interest,
                        ThreadStatus.OPEN.value,
                        proposal.opened_at.isoformat(),
                        retention_until.isoformat(),
                        origins,
                        recorded,
                        expires,
                        references,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ThreadAlreadyExists(proposal.thread_id) from error
        return self.read_thread(proposal.thread_id)

    def set_status(self, thread_id: str, status: ThreadStatus) -> ThreadSnapshot:
        if not isinstance(status, ThreadStatus):
            raise TypeError("status must be a ThreadStatus")
        with self._connection:
            changed = self._connection.execute(
                "UPDATE research_threads SET status = ? WHERE thread_id = ?",
                (status.value, thread_id),
            ).rowcount
        if not changed:
            raise ThreadNotFound(thread_id)
        return self.read_thread(thread_id)

    def read_thread(self, thread_id: str) -> ThreadSnapshot:
        row = self._connection.execute(
            "SELECT * FROM research_threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        if row is None:
            raise ThreadNotFound(thread_id)
        entries = tuple(
            self._entry(item["entry_id"])
            for item in self._connection.execute(
                "SELECT entry_id FROM research_entries WHERE thread_id = ? "
                "ORDER BY rowid",
                (thread_id,),
            )
        )
        return self._thread(row, entries)

    # ---- entries --------------------------------------------------------

    def record_entry(self, proposal: EntryProposal) -> EntrySnapshot:
        status = self._status(proposal.thread_id)
        if status is ThreadStatus.ARCHIVED:
            raise ArchivedThreadWrite(proposal.thread_id)
        origins, recorded, expires, references = provenance_to_storage(
            proposal.provenance
        )
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO research_entries(entry_id, thread_id, kind) "
                    "VALUES (?, ?, ?)",
                    (proposal.entry_id, proposal.thread_id, proposal.kind.value),
                )
                self._connection.execute(
                    "INSERT INTO research_entry_revisions(entry_id, revision, "
                    "content, reason, recorded_at, source_references, "
                    "content_origins, content_recorded_at, content_expires_at, "
                    "mail_references) VALUES (?, 1, ?, NULL, ?, ?, ?, ?, ?, ?)",
                    (
                        proposal.entry_id,
                        proposal.content,
                        proposal.recorded_at.isoformat(),
                        json.dumps(list(proposal.source_references)),
                        origins,
                        recorded,
                        expires,
                        references,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise EntryAlreadyExists(proposal.entry_id) from error
        return self._entry(proposal.entry_id)

    def revise_entry(
        self,
        entry_id: str,
        revision: EntryRevisionProposal,
        expected_revision: int,
    ) -> EntrySnapshot:
        """Add a version. What AL/X thought before is preserved, not replaced."""
        current = self._entry(entry_id)
        if self._status(current.thread_id) is ThreadStatus.ARCHIVED:
            raise ArchivedThreadWrite(current.thread_id)
        if current.revision != expected_revision:
            raise EntryRevisionConflict(
                f"{entry_id} is at revision {current.revision}, "
                f"not {expected_revision}"
            )
        origins, recorded, expires, references = provenance_to_storage(
            revision.provenance
        )
        with self._connection:
            self._connection.execute(
                "INSERT INTO research_entry_revisions(entry_id, revision, content, "
                "reason, recorded_at, source_references, content_origins, "
                "content_recorded_at, content_expires_at, mail_references) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry_id,
                    current.revision + 1,
                    revision.content,
                    revision.reason,
                    revision.recorded_at.isoformat(),
                    json.dumps(list(revision.source_references)),
                    origins,
                    recorded,
                    expires,
                    references,
                ),
            )
        return self._entry(entry_id)

    def read_entry(self, entry_id: str) -> EntrySnapshot:
        return self._entry(entry_id)

    # ---- retrieval ------------------------------------------------------

    def retrieve(self, query: ResearchQuery) -> tuple[EntrySnapshot, ...]:
        """Return only entries matching a scope AL/X chose.

        `ResearchQuery` refuses to be built without a scope, so there is no
        path here that returns the whole notebook.
        """
        clauses = ["1 = 1"]
        values: list[object] = []
        if query.thread_ids:
            marks = ",".join("?" for _ in query.thread_ids)
            clauses.append(f"e.thread_id IN ({marks})")
            values.extend(query.thread_ids)
        if query.kinds:
            marks = ",".join("?" for _ in query.kinds)
            clauses.append(f"e.kind IN ({marks})")
            values.extend(item.value for item in query.kinds)
        if query.statuses:
            marks = ",".join("?" for _ in query.statuses)
            clauses.append(f"t.status IN ({marks})")
            values.extend(item.value for item in query.statuses)
        elif not query.include_archived:
            clauses.append("t.status != ?")
            values.append(ThreadStatus.ARCHIVED.value)
        if query.recorded_after is not None:
            clauses.append("latest.recorded_at >= ?")
            values.append(query.recorded_after.isoformat())
        if query.recorded_before is not None:
            clauses.append("latest.recorded_at <= ?")
            values.append(query.recorded_before.isoformat())
        rows = self._connection.execute(
            "SELECT e.entry_id FROM research_entries e "
            "JOIN research_threads t ON t.thread_id = e.thread_id "
            "JOIN (SELECT entry_id, MAX(revision) AS revision FROM "
            "research_entry_revisions GROUP BY entry_id) top "
            "ON top.entry_id = e.entry_id "
            "JOIN research_entry_revisions latest ON latest.entry_id = e.entry_id "
            "AND latest.revision = top.revision "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY latest.recorded_at DESC, e.rowid DESC LIMIT ?",
            (*values, query.limit),
        ).fetchall()
        found = tuple(self._entry(row["entry_id"]) for row in rows)
        if not query.source_references:
            return found
        # A source match is on the entry's current references, so an entry that
        # cited something and has since been revised away from it is not
        # returned for that source.
        wanted = set(query.source_references)
        return tuple(
            item
            for item in found
            if wanted.intersection(item.current.source_references)
        )

    # ---- correction and deletion ---------------------------------------

    def delete_entry(self, entry_id: str, deleted_at: datetime) -> DeletionRecord:
        """Remove one entry and every version of it.

        The revisions are deleted, not hidden. What remains is the identifier
        and the time, which cannot reconstruct what was written.
        """
        _aware(deleted_at, "deleted_at")
        with self._connection:
            changed = self._connection.execute(
                "DELETE FROM research_entries WHERE entry_id = ?", (entry_id,)
            ).rowcount
            if not changed:
                raise EntryNotFound(entry_id)
            # ON DELETE CASCADE removes the revisions; this is belt and braces
            # for a connection where foreign keys were not enforced.
            self._connection.execute(
                "DELETE FROM research_entry_revisions WHERE entry_id = ?",
                (entry_id,),
            )
            self._connection.execute(
                "INSERT OR REPLACE INTO research_deletions(record_id, kind, "
                "deleted_at) VALUES (?, 'entry', ?)",
                (entry_id, deleted_at.isoformat()),
            )
        return DeletionRecord(entry_id, "entry", deleted_at)

    def delete_thread(self, thread_id: str, deleted_at: datetime) -> DeletionRecord:
        """Remove a thread, its entries and all their versions."""
        _aware(deleted_at, "deleted_at")
        with self._connection:
            entry_ids = [
                row["entry_id"]
                for row in self._connection.execute(
                    "SELECT entry_id FROM research_entries WHERE thread_id = ?",
                    (thread_id,),
                )
            ]
            changed = self._connection.execute(
                "DELETE FROM research_threads WHERE thread_id = ?", (thread_id,)
            ).rowcount
            if not changed:
                raise ThreadNotFound(thread_id)
            for entry_id in entry_ids:
                self._connection.execute(
                    "DELETE FROM research_entry_revisions WHERE entry_id = ?",
                    (entry_id,),
                )
                self._connection.execute(
                    "DELETE FROM research_entries WHERE entry_id = ?", (entry_id,)
                )
                self._connection.execute(
                    "INSERT OR REPLACE INTO research_deletions(record_id, kind, "
                    "deleted_at) VALUES (?, 'entry', ?)",
                    (entry_id, deleted_at.isoformat()),
                )
            self._connection.execute(
                "INSERT OR REPLACE INTO research_deletions(record_id, kind, "
                "deleted_at) VALUES (?, 'thread', ?)",
                (thread_id, deleted_at.isoformat()),
            )
        return DeletionRecord(thread_id, "thread", deleted_at)

    def deletions(self) -> tuple[DeletionRecord, ...]:
        return tuple(
            DeletionRecord(
                row["record_id"], row["kind"], datetime.fromisoformat(row["deleted_at"])
            )
            for row in self._connection.execute(
                "SELECT record_id, kind, deleted_at FROM research_deletions "
                "ORDER BY deleted_at"
            )
        )

    def purge_expired(self, now: datetime) -> tuple[str, ...]:
        """Delete threads whose retention has elapsed.

        Retention is a deadline, not a suggestion. A thread past it is removed
        with its entries, the same as an explicit deletion.
        """
        _aware(now, "now")
        expired = [
            row["thread_id"]
            for row in self._connection.execute(
                "SELECT thread_id FROM research_threads WHERE retention_until <= ?",
                (now.isoformat(),),
            )
        ]
        for thread_id in expired:
            self.delete_thread(thread_id, now)
        return tuple(expired)

    # ---- internals ------------------------------------------------------

    def _status(self, thread_id: str) -> ThreadStatus:
        row = self._connection.execute(
            "SELECT status FROM research_threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        if row is None:
            raise ThreadNotFound(thread_id)
        return ThreadStatus(row["status"])

    def _thread(
        self, row: sqlite3.Row, entries: tuple[EntrySnapshot, ...]
    ) -> ThreadSnapshot:
        return ThreadSnapshot(
            thread_id=row["thread_id"],
            question=row["question"],
            interest=row["interest"],
            status=ThreadStatus(row["status"]),
            opened_at=datetime.fromisoformat(row["opened_at"]),
            retention_until=datetime.fromisoformat(row["retention_until"]),
            entries=entries,
        )

    def _entry(self, entry_id: str) -> EntrySnapshot:
        row = self._connection.execute(
            "SELECT entry_id, thread_id, kind FROM research_entries "
            "WHERE entry_id = ?",
            (entry_id,),
        ).fetchone()
        if row is None:
            raise EntryNotFound(entry_id)
        revisions = tuple(
            EntryRevision(
                revision=item["revision"],
                content=item["content"],
                recorded_at=datetime.fromisoformat(item["recorded_at"]),
                source_references=tuple(json.loads(item["source_references"])),
                reason=item["reason"],
                provenance=provenance_from_storage(
                    item["content_origins"],
                    item["content_recorded_at"],
                    item["content_expires_at"],
                    item["mail_references"],
                ),
            )
            for item in self._connection.execute(
                "SELECT * FROM research_entry_revisions WHERE entry_id = ? "
                "ORDER BY revision",
                (entry_id,),
            )
        )
        if not revisions:
            raise EntryNotFound(entry_id)
        return EntrySnapshot(
            entry_id=row["entry_id"],
            thread_id=row["thread_id"],
            kind=EntryKind(row["kind"]),
            revisions=revisions,
        )
