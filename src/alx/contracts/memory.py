"""Provider-neutral records for AL/X's durable memory boundary.

The records validate shape, provenance, and isolation. They deliberately do
not decide whether an experience is significant; that judgement belongs to
the authoritative Core before an autobiographical proposal is constructed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from alx.contracts.scope import ScopeReference

if TYPE_CHECKING:
    from alx.contracts.provenance import ContentProvenance


def _required(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")


def _aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _references(values: tuple[str, ...]) -> tuple[str, ...]:
    result = tuple(values)
    if not result or any(not value.strip() for value in result):
        raise ValueError("source_references must contain real non-blank references")
    return result


class MemoryKind(str, Enum):
    FACTUAL = "factual"
    RELATIONSHIP = "relationship"
    AUTOBIOGRAPHICAL = "autobiographical"


class MemorySourceMatch(str, Enum):
    ANY = "any"
    ALL = "all"


# The most memories one retrieval may return. Topic retrieval is ranked rather
# than exact, so without a ceiling a vague topic would drift back towards the
# whole-store replay that `IDENTITY_AND_MEMORY.md` forbids. Every retrieval is
# capped, not only topic retrieval, because a wide date range is just as broad.
MAX_RETRIEVAL_LIMIT = 25


class MemoryMatchReason(str, Enum):
    """Why one memory surfaced, in terms of cognition rather than mechanism.

    Deliberately coarse. It says whether a result is a certainty or a
    suggestion, which is what AL/X needs in order to weigh it, and nothing
    about how the suggestion was produced. A value naming the backend —
    "lexical", "semantic", "fts" — would let her reason about the retrieval
    machinery, and the machinery is meant to be replaceable without her
    noticing.
    """

    # Named outright: an identifier or source reference she supplied.
    EXACT = "exact"
    # Fell inside a deterministic boundary she set, such as a date range.
    SCOPE = "scope"
    # Matched what she asked about. A suggestion, never a certainty.
    TOPIC = "topic"


class MemorySupersession(str, Enum):
    """Whether a retrieved memory is the current one or a historical one.

    Retrieval reports this; it never acts on it. Deciding which of two
    conflicting memories is true is a question about meaning, and under Law 3
    that belongs to the Core.
    """

    # Nothing has replaced this memory.
    CURRENT = "current"
    # A later memory replaced this one. It was true; it may not be now.
    SUPERSEDED = "superseded"


class MemoryIdentityConflict(Exception):
    """A proposed memory_id already names a memory with different content.

    Defined here rather than in the store because it is not a storage fault:
    it is a question about meaning that only the Core may answer, so the
    reasoning loop has to recognise it without importing the memory package.
    """


@dataclass(frozen=True, slots=True)
class MemoryQuery:
    """Structured retrieval scope chosen semantically by the AL/X Core."""

    query_id: str
    kinds: tuple[MemoryKind, ...] = ()
    memory_ids: tuple[str, ...] = ()
    person_id: str | None = None
    formed_after: datetime | None = None
    formed_before: datetime | None = None
    source_references: tuple[str, ...] = ()
    source_match: MemorySourceMatch = MemorySourceMatch.ANY
    include_superseded: bool = False
    # What the retrieval is about. The one ranking dimension: it generates
    # candidates rather than bounding them, so it is always paired with the
    # ceiling below. Blank is not a topic, and whitespace is not a topic.
    topic: str | None = None
    # Which project's records to look in. A deterministic boundary applied
    # before any ranking, so a memory belonging elsewhere cannot surface by
    # matching a topic well. Omitting it searches every project and the
    # unscoped memories together, which is what cross-project recall needs.
    project_id: str | None = None
    # The ceiling on one retrieval. Present on every query, because a broad
    # date range replays as much of the store as a vague topic would.
    limit: int = MAX_RETRIEVAL_LIMIT

    def __post_init__(self) -> None:
        _required(self.query_id, "query_id")
        object.__setattr__(self, "kinds", tuple(self.kinds))
        object.__setattr__(self, "memory_ids", tuple(self.memory_ids))
        object.__setattr__(self, "source_references", tuple(self.source_references))
        if any(not isinstance(item, MemoryKind) for item in self.kinds):
            raise TypeError("kinds must contain only MemoryKind values")
        if not self.kinds:
            raise ValueError("retrieval requires at least one memory kind")
        if len(self.kinds) != len(set(self.kinds)):
            raise ValueError("kinds must not contain duplicates")
        if any(not item.strip() for item in self.memory_ids):
            raise ValueError("memory_ids must not contain blanks")
        if len(self.memory_ids) != len(set(self.memory_ids)):
            raise ValueError("memory_ids must not contain duplicates")
        if any(not item.strip() for item in self.source_references):
            raise ValueError("source_references must not contain blanks")
        if len(self.source_references) != len(set(self.source_references)):
            raise ValueError("source_references must not contain duplicates")
        if self.person_id is not None:
            _required(self.person_id, "person_id")
        if self.formed_after is not None:
            _aware(self.formed_after, "formed_after")
        if self.formed_before is not None:
            _aware(self.formed_before, "formed_before")
        if (
            self.formed_after is not None
            and self.formed_before is not None
            and self.formed_after > self.formed_before
        ):
            raise ValueError("formed_after must not be later than formed_before")
        if not isinstance(self.source_match, MemorySourceMatch):
            raise TypeError("source_match must be a MemorySourceMatch")
        if not isinstance(self.include_superseded, bool):
            raise TypeError("include_superseded must be boolean")
        if self.topic is not None:
            if not isinstance(self.topic, str):
                raise TypeError("topic must be a string or None")
            # Normalised before it is judged, so " " cannot pass as a topic and
            # then match everything once the backend trims it.
            normalised = " ".join(self.topic.split())
            if not normalised:
                raise ValueError("topic must not be blank")
            object.__setattr__(self, "topic", normalised)
        if self.project_id is not None:
            _required(self.project_id, "project_id")
        if not isinstance(self.limit, int) or isinstance(self.limit, bool):
            raise TypeError("limit must be an int")
        if self.limit < 1:
            raise ValueError("limit must be positive")
        if self.limit > MAX_RETRIEVAL_LIMIT:
            raise ValueError(
                f"limit must not exceed {MAX_RETRIEVAL_LIMIT}"
            )
        if MemoryKind.RELATIONSHIP in self.kinds and self.person_id is None:
            raise ValueError("relationship retrieval requires person_id")
        if self.person_id is not None and MemoryKind.RELATIONSHIP not in self.kinds:
            raise ValueError("person_id is only a relationship-memory retrieval boundary")
        if (
            self.person_id is not None
            and any(kind is not MemoryKind.RELATIONSHIP for kind in self.kinds)
            and not any(
                (
                    self.memory_ids,
                    self.formed_after,
                    self.formed_before,
                    self.source_references,
                    self.topic,
                )
            )
        ):
            raise ValueError("non-relationship kinds require their own retrieval scope")
        # A retrieval must say more than which kinds of memory it wants. Kinds
        # alone would replay the store, which is the whole-history replay
        # `IDENTITY_AND_MEMORY.md` forbids, and refusing it once ended two live
        # turns mid-sentence because the protocol never said how to narrow.
        #
        # A topic now satisfies this. It is not an exact boundary, but it is a
        # genuine narrowing: results are ranked against something she asked
        # about and capped by `limit`, so the answer to a vague topic is the
        # best few, never the store.
        #
        # `project_id` deliberately does not satisfy it. A project accumulates
        # indefinitely, so "everything in this project" is the same replay
        # wearing a scope. It narrows a retrieval; it cannot be the whole of
        # one.
        if not any(
            (
                self.memory_ids,
                self.person_id,
                self.formed_after,
                self.formed_before,
                self.source_references,
                self.topic,
            )
        ):
            raise ValueError("retrieval requires a scope narrower than memory kind alone")


@dataclass(frozen=True, slots=True)
class MemoryProposal:
    """A semantic memory judgement already made by the AL/X Core."""

    memory_id: str
    kind: MemoryKind
    content: str
    source_references: tuple[str, ...]
    formed_at: datetime
    person_id: str | None = None
    meaning: str | None = None
    supersedes_memory_id: str | None = None
    provenance: ContentProvenance | None = None
    # Where this memory belongs, when the Core chose to say. Optional
    # everywhere: memory is platform-wide, so an unscoped memory is ordinary
    # rather than incomplete, and a scope never narrows who may read it.
    # Person isolation remains `person_id` alone.
    scope: ScopeReference | None = None

    def __post_init__(self) -> None:
        _required(self.memory_id, "memory_id")
        if not isinstance(self.kind, MemoryKind):
            raise TypeError("kind must be a MemoryKind")
        _required(self.content, "content")
        object.__setattr__(self, "source_references", _references(self.source_references))
        _aware(self.formed_at, "formed_at")
        if self.kind is MemoryKind.RELATIONSHIP:
            if self.person_id is None:
                raise ValueError("relationship memory requires person_id")
            _required(self.person_id, "person_id")
        elif self.person_id is not None:
            raise ValueError("person_id is reserved for relationship memory")
        if self.kind is MemoryKind.AUTOBIOGRAPHICAL:
            if self.meaning is None:
                raise ValueError("autobiographical memory requires the Core's meaning reflection")
            _required(self.meaning, "meaning")
        elif self.meaning is not None:
            raise ValueError("meaning is reserved for autobiographical memory")
        if self.supersedes_memory_id is not None:
            _required(self.supersedes_memory_id, "supersedes_memory_id")
            if self.supersedes_memory_id == self.memory_id:
                raise ValueError("a memory cannot supersede itself")
        if self.provenance is not None:
            from alx.contracts.provenance import ContentProvenance

            if not isinstance(self.provenance, ContentProvenance):
                raise TypeError("memory provenance must be ContentProvenance or None")
        if self.scope is not None and not isinstance(self.scope, ScopeReference):
            raise TypeError("memory scope must be a ScopeReference or None")


@dataclass(frozen=True, slots=True)
class MemoryCorrection:
    """An explicit Core-authored correction that preserves earlier revisions."""

    content: str
    reason: str
    source_references: tuple[str, ...]
    corrected_at: datetime
    meaning: str | None = None
    provenance: ContentProvenance | None = None

    def __post_init__(self) -> None:
        _required(self.content, "content")
        _required(self.reason, "reason")
        object.__setattr__(self, "source_references", _references(self.source_references))
        _aware(self.corrected_at, "corrected_at")
        if self.meaning is not None:
            _required(self.meaning, "meaning")
        if self.provenance is not None:
            from alx.contracts.provenance import ContentProvenance

            if not isinstance(self.provenance, ContentProvenance):
                raise TypeError("memory provenance must be ContentProvenance or None")


@dataclass(frozen=True, slots=True)
class MemoryRevision:
    revision: int
    content: str
    source_references: tuple[str, ...]
    recorded_at: datetime
    reason: str | None = None
    meaning: str | None = None
    provenance: ContentProvenance | None = None


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    memory_id: str
    kind: MemoryKind
    person_id: str | None
    supersedes_memory_id: str | None
    revisions: tuple[MemoryRevision, ...]
    retention_until: datetime
    # Defaulted so that every existing construction site, and every memory
    # written before scopes existed, stays valid and unscoped.
    scope: ScopeReference | None = None
    # Why this memory surfaced, and whether it is still the current one. Both
    # describe one retrieval rather than the memory itself, so both default:
    # a snapshot loaded by identifier is not the answer to a query and says
    # nothing about either.
    match_reason: MemoryMatchReason | None = None
    supersession: MemorySupersession | None = None

    @property
    def current(self) -> MemoryRevision:
        return self.revisions[-1]

    @property
    def revision(self) -> int:
        return self.current.revision
