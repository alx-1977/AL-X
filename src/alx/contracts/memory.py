"""Provider-neutral records for AL/X's durable memory boundary.

The records validate shape, provenance, and isolation. They deliberately do
not decide whether an experience is significant; that judgement belongs to
the authoritative Core before an autobiographical proposal is constructed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


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


@dataclass(frozen=True, slots=True)
class MemoryCorrection:
    """An explicit Core-authored correction that preserves earlier revisions."""

    content: str
    reason: str
    source_references: tuple[str, ...]
    corrected_at: datetime
    meaning: str | None = None

    def __post_init__(self) -> None:
        _required(self.content, "content")
        _required(self.reason, "reason")
        object.__setattr__(self, "source_references", _references(self.source_references))
        _aware(self.corrected_at, "corrected_at")
        if self.meaning is not None:
            _required(self.meaning, "meaning")


@dataclass(frozen=True, slots=True)
class MemoryRevision:
    revision: int
    content: str
    source_references: tuple[str, ...]
    recorded_at: datetime
    reason: str | None = None
    meaning: str | None = None


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
