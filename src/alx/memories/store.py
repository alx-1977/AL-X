"""SQLite persistence for memories selected semantically by the AL/X Core."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from dataclasses import replace

from alx.contracts import (
    MemoryCorrection,
    MemoryMatchReason,
    MemoryIdentityConflict,
    MemoryKind,
    MemoryProposal,
    MemoryQuery,
    MemoryRevision,
    MemorySnapshot,
    MemorySourceMatch,
    MemorySupersession,
)
from alx.contracts.provenance import (
    ContentProvenance,
    provenance_from_storage,
    provenance_to_storage,
)
from alx.contracts.scope import scope_from_storage, scope_to_storage


LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 3
PROVENANCE_COLUMNS = (
    "content_origins",
    "content_recorded_at",
    "content_expires_at",
    "mail_references",
)


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


class TopicRetrievalUnavailable(MemoryStoreError):
    """A topic retrieval was asked for while the derived index cannot answer.

    Raised rather than answered, because the two possible quiet answers are
    both untrue. Returning nothing would say no memory matches; returning the
    eligible memories unranked would say these are what she asked about. The
    honest fact is that the capability is missing, and only the Core can decide
    what to do about that.

    It is deliberately not raised for any other operation: remembering,
    loading and every exact retrieval carry on untouched.
    """


def _aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _topic_query(topic: str) -> str | None:
    """Turn what AL/X asked about into one safe FTS5 query.

    Every term is quoted and the terms are ANDed. Quoting is what stops FTS5's
    own syntax being read out of her words: a topic containing `OR`, `NEAR` or
    a bare `*` would otherwise change the search into something she did not
    ask for, and an unbalanced quote would make it fail outright.

    ANDing is the deliberately conservative choice. Requiring every term
    returns fewer, more relevant memories and answers a two-word topic with
    memories about both words rather than either, which is nearer to what
    asking about something means.

    Returns None when nothing usable survives, which the caller reads as a
    topic that matches nothing rather than one that matches everything.

    Punctuation inside a term is kept. Deleting it silently changed what was
    being searched for — `13.56 MHz` became `1356`, `1/3 oz` became `13` — and
    both then matched nothing, which in a domain full of part numbers,
    tolerances and frequencies is the worst possible failure: a confident empty
    answer about exactly the things most worth remembering.

    Quoting is what makes that safe. Inside a double-quoted FTS5 string the
    tokenizer splits on punctuation and the operators lose their meaning, so
    `13.56` becomes the phrase "13 56", `MAX17048` and `3V3` survive whole, and
    `OR`, `NEAR`, `*` and `^` are ordinary text. The only character that needs
    handling is the double quote itself, which is escaped by doubling as SQL
    has always done. Nothing here needs to know which punctuation an engineer
    might use, which is what keeps it from being a list someone has to
    maintain.
    """
    usable = [
        '"' + term.replace('"', '""') + '"'
        for term in topic.split()
        # A term of pure punctuation tokenizes to nothing and would make FTS5
        # reject the whole query, taking the usable terms with it.
        if any(character.isalnum() for character in term)
    ]
    return " AND ".join(usable) if usable else None


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


def _decode_revision(
    value: str,
    provenance: ContentProvenance | None = None,
) -> MemoryRevision:
    data = json.loads(value)
    return MemoryRevision(
        revision=data["revision"],
        content=data["content"],
        source_references=tuple(data["source_references"]),
        recorded_at=datetime.fromisoformat(data["recorded_at"]),
        reason=data["reason"],
        meaning=data["meaning"],
        provenance=provenance,
    )


class SQLiteMemoryStore:
    """Validates and persists Core proposals without assessing significance."""

    def __init__(self, database_path: str | Path) -> None:
        # Core turns execute on one serialized worker so blocking provider I/O
        # cannot stall the asyncio voice transport.
        self._connection = sqlite3.connect(str(database_path), check_same_thread=False)
        self._connection.execute("PRAGMA foreign_keys = ON")
        # Whether topic retrieval is possible at all. Set by the migration and
        # cleared if the index later becomes unusable; it is the one place the
        # store records that search is degraded, so nothing else has to guess
        # from a swallowed exception.
        self._topic_index_available = False
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
                "CREATE TABLE IF NOT EXISTS memory_revisions (memory_id TEXT NOT NULL REFERENCES memories(memory_id) ON DELETE CASCADE, revision INTEGER NOT NULL, revision_json TEXT NOT NULL, content_origins TEXT, content_recorded_at TEXT, content_expires_at TEXT, mail_references TEXT, PRIMARY KEY(memory_id, revision))"
            )
            memory_columns = {
                item[1]
                for item in self._connection.execute("PRAGMA table_info(memories)")
            }
            # Additive and nullable: every memory written before scopes
            # existed stays valid and reads back unscoped.
            if "scope" not in memory_columns:
                self._connection.execute("ALTER TABLE memories ADD COLUMN scope TEXT")
            columns = {
                item[1]
                for item in self._connection.execute(
                    "PRAGMA table_info(memory_revisions)"
                )
            }
            for column in PROVENANCE_COLUMNS:
                if column not in columns:
                    self._connection.execute(
                        f'ALTER TABLE memory_revisions ADD COLUMN "{column}" TEXT'
                    )
            self._build_topic_index()
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # ---- topic index ----------------------------------------------------
    #
    # A derived accelerator over the current content of each memory, and
    # nothing else. The authoritative rows are `memories` and
    # `memory_revisions`; this table holds a copy of text those rows already
    # contain, so dropping it loses nothing and rebuilding it from them is
    # always correct. `rebuild_topic_index` exists to make that explicit and
    # testable rather than theoretical.
    #
    # It is an external-content-free FTS5 table rather than one linked to a
    # source table, because the text it indexes is the *current* revision,
    # which is a computed choice over `memory_revisions` rather than a column
    # anyone could point FTS5 at. Keeping it independent means a correction
    # updates one row here instead of leaving the index describing a revision
    # that is no longer current.
    #
    # Every memory is indexed, including one that something later replaced,
    # and only its *current* revision is. Superseded memories stay in the index
    # deliberately: whether history is wanted is `include_superseded`, a
    # deterministic filter AL/X sets, and an index that quietly dropped those
    # rows would make asking for history return nothing while appearing to
    # work. The index decides relevance; it never decides what is true now.

    def _build_topic_index(self) -> None:
        """Create the index and fill it from the memories already stored.

        Backfill is what makes this safe to add to a database that predates it.
        The table is created empty, so without this an existing memory would
        stay invisible to topic retrieval while loading perfectly by
        identifier — search that silently knows nothing about everything
        remembered so far, which is worse than search that is plainly absent.

        It is idempotent and costs nothing on an already-populated index: the
        backfill runs only when the index holds no rows, so reopening a
        migrated database does not rebuild it. An index emptied or damaged
        later is repaired by `rebuild_topic_index`, which is explicit.

        Failure here is not failure of the store. FTS5 is compiled into SQLite
        rather than guaranteed by it, so a runtime without it must still be
        able to remember; the flag records that topic retrieval is unavailable
        and every other operation carries on.
        """
        try:
            self._connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memory_topics USING fts5("
                "memory_id UNINDEXED, content, tokenize='unicode61')"
            )
            indexed = self._connection.execute(
                "SELECT EXISTS(SELECT 1 FROM memory_topics)"
            ).fetchone()[0]
            # Marked available before the backfill, because the backfill writes
            # through the same guarded path every other write uses and that
            # path declines to act while the index is considered unavailable.
            self._topic_index_available = True
            if not indexed:
                for (memory_id,) in self._connection.execute(
                    "SELECT memory_id FROM memories ORDER BY memory_id"
                ).fetchall():
                    self._index_memory(memory_id)
        except sqlite3.OperationalError:
            LOGGER.warning(
                "Topic retrieval is unavailable: this SQLite has no FTS5. "
                "Memory itself is unaffected."
            )
            self._topic_index_available = False

    def rebuild_topic_index(self) -> None:
        """Discard the derived index and recreate it from the memories.

        Safe at any time: it touches no authoritative row. It exists so that
        an index which is absent, stale or damaged is a recoverable condition
        rather than a reason to doubt what is remembered.
        """
        with self._connection:
            try:
                self._connection.execute("DROP TABLE IF EXISTS memory_topics")
            except sqlite3.OperationalError:
                self._topic_index_available = False
                return
            self._build_topic_index()

    def _index_memory(self, memory_id: str) -> None:
        """Record one memory's current content, replacing what was there.

        A no-op when there is no index. Remembering must never fail because
        the thing that makes memories findable by topic is missing.
        """
        if not self._topic_index_available:
            return
        row = self._connection.execute(
            "SELECT revision_json FROM memory_revisions WHERE memory_id = ? "
            "ORDER BY revision DESC LIMIT 1",
            (memory_id,),
        ).fetchone()
        if row is None:
            return
        content = json.loads(row[0]).get("content")
        try:
            self._connection.execute(
                "DELETE FROM memory_topics WHERE memory_id = ?", (memory_id,)
            )
            if isinstance(content, str) and content.strip():
                self._connection.execute(
                    "INSERT INTO memory_topics(memory_id, content) VALUES (?, ?)",
                    (memory_id, content),
                )
        except sqlite3.OperationalError:
            # The index vanished under us. The memory is still stored, so this
            # degrades search and nothing else.
            self._topic_index_available = False

    def _forget_topic(self, memory_ids: tuple[str, ...]) -> None:
        """Drop index rows for memories that no longer exist."""
        if not self._topic_index_available:
            return
        try:
            self._connection.executemany(
                "DELETE FROM memory_topics WHERE memory_id = ?",
                ((item,) for item in memory_ids),
            )
        except sqlite3.OperationalError:
            self._topic_index_available = False

    def _topic_matches(self, topic: str) -> list[str] | None:
        """Memory identifiers whose current content matches, best first.

        None means the index could not answer, which is a different fact from
        an empty list and is reported as such rather than being passed off as
        "nothing matched".
        """
        if not self._topic_index_available:
            return None
        query = _topic_query(topic)
        if query is None:
            return []
        try:
            rows = self._connection.execute(
                "SELECT memory_id FROM memory_topics WHERE memory_topics "
                "MATCH ? ORDER BY rank",
                (query,),
            ).fetchall()
        except sqlite3.OperationalError:
            self._topic_index_available = False
            return None
        return [item[0] for item in rows]

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
            "SELECT kind, person_id, supersedes_memory_id, retention_until, scope FROM memories WHERE memory_id = ?",
            (memory_id,),
        ).fetchone()
        if row is None:
            raise MemoryNotFound(memory_id)
        revisions = tuple(
            _decode_revision(item[0], provenance_from_storage(*item[1:]))
            for item in self._connection.execute(
                "SELECT revision_json, content_origins, content_recorded_at, "
                "content_expires_at, mail_references FROM memory_revisions "
                "WHERE memory_id = ? ORDER BY revision",
                (memory_id,),
            )
        )
        return MemorySnapshot(
            memory_id, MemoryKind(row[0]), row[1], row[2], revisions,
            datetime.fromisoformat(row[3]), scope_from_storage(row[4]),
        )

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
        """Apply Core-selected metadata constraints without interpreting meaning.

        Order matters and is deliberate. Every deterministic constraint — kind,
        identifier, person, project, date, source, supersession — decides which
        memories are eligible. Only then does a topic order what survived. A
        topic can therefore never widen a boundary AL/X set, which is what
        keeps a good match in another project, or another person's memory, from
        surfacing because the words happened to fit.

        Nothing here interprets what a memory means or which of two memories is
        right. It marks each result as current or superseded and returns them
        both when asked; the judgement is the Core's.
        """
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
            if query.project_id is not None:
                scope = item.scope
                if scope is None or scope.project_id != query.project_id:
                    continue
            if not query.include_superseded and item.memory_id in superseded_ids:
                continue
            selected.append(item)

        if query.topic is not None:
            ranked = self._topic_matches(query.topic)
            if ranked is None:
                # Neither quiet answer is true: an empty result would claim
                # nothing matched, and the unranked eligible set would claim
                # these are what she asked about. Say what is actually wrong.
                raise TopicRetrievalUnavailable(
                    "topic retrieval requires the derived index, which this "
                    "SQLite cannot provide"
                )
            else:
                position = {
                    memory_id: index for index, memory_id in enumerate(ranked)
                }
                selected = [
                    item for item in selected if item.memory_id in position
                ]
                selected.sort(key=lambda item: position[item.memory_id])
                reason = MemoryMatchReason.TOPIC
        elif query.memory_ids or query.source_references:
            reason = MemoryMatchReason.EXACT
        else:
            reason = MemoryMatchReason.SCOPE

        return tuple(
            replace(
                item,
                match_reason=reason,
                supersession=(
                    MemorySupersession.SUPERSEDED
                    if item.memory_id in superseded_ids
                    else MemorySupersession.CURRENT
                ),
            )
            for item in selected[: query.limit]
        )

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
            correction.provenance or current.current.provenance,
        )
        self._validate_replacement_provenance(
            current.current.provenance, revision.provenance
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
                    "INSERT INTO memory_revisions(memory_id, revision, revision_json, content_origins, content_recorded_at, content_expires_at, mail_references) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        memory_id,
                        revision.revision,
                        _encode_revision(revision),
                        *provenance_to_storage(revision.provenance),
                    ),
                )
                # The current revision changed, so what the index describes
                # must change with it.
                self._index_memory(memory_id)
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
            self._forget_topic((memory_id,))

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
            self._forget_topic(identifiers)
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
            provenance=proposal.provenance,
        )
        self._connection.execute(
            "INSERT INTO memories(memory_id, kind, person_id, supersedes_memory_id, retention_until, scope) VALUES (?, ?, ?, ?, ?, ?)",
            (proposal.memory_id, proposal.kind.value, proposal.person_id, proposal.supersedes_memory_id, retention_until.isoformat(), scope_to_storage(proposal.scope)),
        )
        self._connection.execute(
            "INSERT INTO memory_revisions(memory_id, revision, revision_json, content_origins, content_recorded_at, content_expires_at, mail_references) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                proposal.memory_id,
                1,
                _encode_revision(revision),
                *provenance_to_storage(proposal.provenance),
            ),
        )
        # Inside the caller's transaction, so the index cannot record a memory
        # that was not stored.
        self._index_memory(proposal.memory_id)

    @staticmethod
    def _matches_proposal(
        existing: MemorySnapshot,
        proposal: MemoryProposal,
        retention_until: datetime,
    ) -> bool:
        """Whether a proposal is the memory already stored under that identifier.

        Every field that constitutes the memory is compared: what is
        remembered, who it concerns, where it belongs, what it came from, when
        it was formed and what it means. Provenance is deliberately not among
        them. It describes the reasoning step that produced the proposal, not
        the fact being remembered, and the Core stamps a fresh one on every
        step: its recorded_at is that step's clock and its mail references grow
        as messages arrive. Comparing it made this guard unreachable, so a
        repeated identifier raised MemoryIdentityConflict, the Core returned
        memory_persistence_error and the conversation ended mid-sentence.

        A memory that differs in any of these fields is a different memory and
        still conflicts, which is what stops one identifier quietly coming to
        mean something else.
        """
        initial = existing.revisions[0]
        return (
            existing.kind is proposal.kind
            and existing.person_id == proposal.person_id
            and existing.scope == proposal.scope
            and existing.supersedes_memory_id == proposal.supersedes_memory_id
            and initial.content == proposal.content
            and initial.source_references == proposal.source_references
            and initial.recorded_at == proposal.formed_at
            and initial.meaning == proposal.meaning
            and existing.retention_until == retention_until
        )

    @staticmethod
    def _validate_replacement_provenance(
        existing: ContentProvenance | None,
        proposed: ContentProvenance | None,
    ) -> None:
        if existing is None:
            return
        if proposed is None or not existing.origins.issubset(proposed.origins):
            raise ValueError("memory correction cannot discard provenance origins")
        if not set(existing.mail_references).issubset(proposed.mail_references):
            raise ValueError("memory correction cannot discard mail references")
        if (
            existing.content_expires_at is not None
            and (
                proposed.content_expires_at is None
                or proposed.content_expires_at > existing.content_expires_at
            )
        ):
            raise ValueError("memory correction cannot extend content retention")
