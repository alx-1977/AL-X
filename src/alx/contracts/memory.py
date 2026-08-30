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
                )
            )
        ):
            raise ValueError("non-relationship kinds require their own retrieval scope")
        if not any(
            (
                self.memory_ids,
                self.person_id,
                self.formed_after,
                self.formed_before,
                self.source_references,
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

    @property
    def current(self) -> MemoryRevision:
        return self.revisions[-1]

    @property
    def revision(self) -> int:
        return self.current.revision
