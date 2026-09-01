"""Records for AL/X's durable research notebook.

The notebook gives research intellectual continuity across restarts. It is
storage, not a second mind: it holds no goal, plans nothing, wakes nothing, and
decides nothing. A thread records what AL/X is investigating and, in her own
words, why it interests her; entries record what she has come to think and how
that thinking changed.

Three boundaries are deliberate. Evidence is referenced, never copied, so the
notebook cannot become a second evidence store. A thread has no objective,
success criteria or next action, so it cannot become a second goal store. And
nothing here promotes anything into autobiographical memory: whether a research
experience mattered to her is a judgement only the Core makes, through the
memory path that already exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
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


def _references(values: tuple[str, ...], name: str = "source_references") -> tuple[str, ...]:
    result = tuple(values)
    if any(not value.strip() for value in result):
        raise ValueError(f"{name} must not contain blanks")
    if len(result) != len(set(result)):
        raise ValueError(f"{name} must not contain duplicates")
    return result


class ThreadStatus(str, Enum):
    """Where a line of enquiry stands.

    There is no "complete". A conclusion is something AL/X writes as an entry
    and may later revise; a status that declared research finished would invite
    exactly the closed-off thinking the notebook exists to avoid.
    """

    OPEN = "open"
    PAUSED = "paused"
    ARCHIVED = "archived"


class RevisionAuthor(str, Enum):
    """Who wrote a version of an entry.

    AL/X changing her own mind and Friedl correcting the record are different
    events with different authority, and a history that blurred them would let a
    model's revision be read later as the owner's correction.
    """

    ALX = "alx"
    FRIEDL = "friedl"


class EntryKind(str, Enum):
    """What kind of thinking an entry records.

    Doubts and open questions are first-class. Research that could only record
    settled claims would lose the part worth keeping.
    """

    CLAIM = "claim"
    HYPOTHESIS = "hypothesis"
    DOUBT = "doubt"
    QUESTION = "question"
    CONCLUSION = "conclusion"


@dataclass(frozen=True, slots=True)
class ThreadProposal:
    """A line of enquiry AL/X has decided to open."""

    thread_id: str
    question: str
    # Why this interests her, in her own words. Required: a research notebook
    # that recorded only subjects would lose the reason she chose them, which
    # is the part that makes the enquiry hers rather than assigned.
    interest: str
    opened_at: datetime
    provenance: "ContentProvenance | None" = None

    def __post_init__(self) -> None:
        _required(self.thread_id, "thread_id")
        _required(self.question, "question")
        _required(self.interest, "interest")
        _aware(self.opened_at, "opened_at")
        if self.provenance is not None:
            from alx.contracts.provenance import ContentProvenance

            if not isinstance(self.provenance, ContentProvenance):
                raise TypeError("thread provenance must be ContentProvenance or None")


@dataclass(frozen=True, slots=True)
class EntryProposal:
    """One thought recorded against a thread."""

    entry_id: str
    thread_id: str
    kind: EntryKind
    content: str
    recorded_at: datetime
    # Identifiers of evidence gathered through existing read capabilities. The
    # notebook points at that evidence; copying it here would make this a
    # second evidence store with its own diverging copy.
    source_references: tuple[str, ...] = ()
    provenance: "ContentProvenance | None" = None

    def __post_init__(self) -> None:
        _required(self.entry_id, "entry_id")
        _required(self.thread_id, "thread_id")
        if not isinstance(self.kind, EntryKind):
            raise TypeError("kind must be an EntryKind")
        _required(self.content, "content")
        _aware(self.recorded_at, "recorded_at")
        object.__setattr__(self, "source_references", _references(self.source_references))
        if self.provenance is not None:
            from alx.contracts.provenance import ContentProvenance

            if not isinstance(self.provenance, ContentProvenance):
                raise TypeError("entry provenance must be ContentProvenance or None")


@dataclass(frozen=True, slots=True)
class EntryRevision:
    """One version of an entry. Earlier versions are never overwritten."""

    revision: int
    content: str
    recorded_at: datetime
    source_references: tuple[str, ...] = ()
    author: RevisionAuthor = RevisionAuthor.ALX
    # Why the view changed. Absent on the first revision, required after it:
    # a changed conclusion without a reason loses what the revision was for.
    reason: str | None = None
    provenance: "ContentProvenance | None" = None


@dataclass(frozen=True, slots=True)
class EntryRevisionProposal:
    """A changed view, preserving what AL/X thought before."""

    content: str
    reason: str
    recorded_at: datetime
    source_references: tuple[str, ...] = ()
    provenance: "ContentProvenance | None" = None

    def __post_init__(self) -> None:
        _required(self.content, "content")
        _required(self.reason, "reason")
        _aware(self.recorded_at, "recorded_at")
        object.__setattr__(self, "source_references", _references(self.source_references))


@dataclass(frozen=True, slots=True)
class EntrySnapshot:
    entry_id: str
    thread_id: str
    kind: EntryKind
    revisions: tuple[EntryRevision, ...]

    @property
    def current(self) -> EntryRevision:
        return self.revisions[-1]

    @property
    def revision(self) -> int:
        return self.current.revision


@dataclass(frozen=True, slots=True)
class ThreadSnapshot:
    thread_id: str
    question: str
    interest: str
    status: ThreadStatus
    opened_at: datetime
    retention_until: datetime
    entries: tuple[EntrySnapshot, ...] = ()
    # How many entries the thread actually holds, and where a further read
    # should continue from. A thread larger than the bound is paged, never
    # returned whole.
    total_entries: int = 0
    next_offset: int | None = None


# Hard retrieval bounds. Core context is the scarce resource the notebook exists
# to protect, so no query, however scoped, may return more than this.
MAX_RETRIEVAL_LIMIT = 25
# A window wide enough is not a scope. Ninety days is long enough to find a
# thread worked on last quarter and far short of "everything".
MAX_WINDOW_DAYS = 90
# One read of a thread returns at most this many entries, and at most this many
# revisions of each, with continuation information when more exist.
MAX_THREAD_ENTRIES = 25
MAX_ENTRY_REVISIONS = 10


@dataclass(frozen=True, slots=True)
class ResearchQuery:
    """Structured retrieval scope, chosen semantically by the Core.

    A scope is mandatory. Without one this would become a list-all path, and
    the notebook would end up injected wholesale into Core context — the exact
    cost the notebook is meant to avoid. Asking for "every claim" is not a
    scope; asking for the claims in one thread, or citing one source, is.
    """

    query_id: str
    thread_ids: tuple[str, ...] = ()
    kinds: tuple[EntryKind, ...] = ()
    source_references: tuple[str, ...] = ()
    recorded_after: datetime | None = None
    recorded_before: datetime | None = None
    statuses: tuple[ThreadStatus, ...] = ()
    # Archived research stays out of retrieval unless asked for by name, so a
    # put-aside enquiry does not keep surfacing in ordinary work.
    include_archived: bool = False
    limit: int = MAX_RETRIEVAL_LIMIT

    def __post_init__(self) -> None:
        _required(self.query_id, "query_id")
        object.__setattr__(self, "thread_ids", _references(self.thread_ids, "thread_ids"))
        object.__setattr__(self, "kinds", tuple(self.kinds))
        object.__setattr__(
            self, "source_references", _references(self.source_references)
        )
        object.__setattr__(self, "statuses", tuple(self.statuses))
        if any(not isinstance(item, EntryKind) for item in self.kinds):
            raise TypeError("kinds must contain only EntryKind values")
        if any(not isinstance(item, ThreadStatus) for item in self.statuses):
            raise TypeError("statuses must contain only ThreadStatus values")
        if len(self.kinds) != len(set(self.kinds)):
            raise ValueError("kinds must not contain duplicates")
        if self.limit <= 0:
            raise ValueError("limit must be positive")
        if self.limit > MAX_RETRIEVAL_LIMIT:
            raise ValueError(
                f"limit must not exceed {MAX_RETRIEVAL_LIMIT}; a larger page "
                "would put an unbounded amount of research into Core context"
            )
        if self.recorded_after is not None:
            _aware(self.recorded_after, "recorded_after")
        if self.recorded_before is not None:
            _aware(self.recorded_before, "recorded_before")
        if (
            self.recorded_after is not None
            and self.recorded_before is not None
            and self.recorded_after > self.recorded_before
        ):
            raise ValueError("recorded_after must not be later than recorded_before")
        # A time window only counts as a scope if it is actually narrow. An
        # open-ended or century-wide window selects the whole notebook, which is
        # the dump this guard exists to prevent.
        if self.recorded_after is not None or self.recorded_before is not None:
            if self.recorded_after is None or self.recorded_before is None:
                raise ValueError(
                    "a time scope requires both recorded_after and recorded_before; "
                    "an open-ended window is not a scope"
                )
            span = self.recorded_before - self.recorded_after
            if span > timedelta(days=MAX_WINDOW_DAYS):
                raise ValueError(
                    f"a time scope must span at most {MAX_WINDOW_DAYS} days"
                )
        # Kind and status alone are not scopes: "every claim" and "everything
        # open" both return the whole notebook.
        if not (
            self.thread_ids
            or self.source_references
            or self.recorded_after is not None
            or self.recorded_before is not None
        ):
            raise ValueError(
                "retrieval requires a scope narrower than kind or status alone"
            )


@dataclass(frozen=True, slots=True)
class DeletionRecord:
    """Proof that research was deleted, carrying none of what was deleted.

    Friedl's deletion removes the research itself. What remains is an
    identifier and a time, so a dangling reference elsewhere can be explained
    rather than silently pointing at nothing. It holds no question, no
    interest, no entry content and no source references, so the deleted
    research cannot be reconstructed from it.
    """

    record_id: str
    kind: str
    deleted_at: datetime

    def __post_init__(self) -> None:
        _required(self.record_id, "record_id")
        if self.kind not in ("thread", "entry"):
            raise ValueError("kind must be thread or entry")
        _aware(self.deleted_at, "deleted_at")
