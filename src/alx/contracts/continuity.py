"""Records for AL/X's own future cognition requests.

This is the mechanism by which AL/X, and not the runtime, sets the rhythm of
her own thinking. She says when she wants another occasion and writes a note to
her future self; deterministic code stores the time, honours it, and hands the
note back unread.

Three boundaries are deliberate.

`note` is hers. It is persisted and returned verbatim, and no deterministic
component may parse, classify, score, keyword-match, summarise or branch on it.
It is a message from AL/X to AL/X, and the moment code starts reading it, code
has begun deciding what she meant.

There is no condition language, and no topic, priority, urgency, category,
purpose, reason or suggested-action field. Every such field would be a place
for a rule about what deserves attention to hide, and the objective runtime
events already create occasions for the mechanical situations a condition
would have expressed.

`not_before` means one thing only: the opportunity may not arise before this
time. It confers no priority, no importance and no ordering beyond time.
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


class FutureCognitionStatus(str, Enum):
    """Where a request stands. Never how much it matters."""

    PENDING = "pending"
    HONOURED = "honoured"
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True, slots=True)
class FutureCognitionRequest:
    """One occasion AL/X asked for, and her private note about why.

    The fields are exhaustive on purpose. A reviewer should be able to see at a
    glance that nothing here describes the subject, the importance or the
    intent of the request, because those are judgements and they stay with her.
    """

    request_id: str
    not_before: datetime
    note: str
    requested_at: datetime
    references: tuple[str, ...] = ()
    status: FutureCognitionStatus = FutureCognitionStatus.PENDING
    provenance: "ContentProvenance | None" = None

    def __post_init__(self) -> None:
        _required(self.request_id, "request_id")
        # A blank note is allowed to be absent, but not whitespace pretending
        # to be content: an empty message to her future self is a real choice,
        # a blank-looking one is a mistake.
        if not isinstance(self.note, str):
            raise TypeError("note must be a string")
        _aware(self.not_before, "not_before")
        _aware(self.requested_at, "requested_at")
        if not isinstance(self.status, FutureCognitionStatus):
            raise TypeError("status must be a FutureCognitionStatus")
        references = tuple(self.references)
        if any(not item.strip() for item in references):
            raise ValueError("references must not contain blanks")
        if len(references) != len(set(references)):
            raise ValueError("references must not contain duplicates")
        object.__setattr__(self, "references", references)


class FutureCognitionError(Exception):
    """A request could not be stored or changed."""


class FutureCognitionNotFound(FutureCognitionError):
    """No request with that exact identity exists."""


class DuplicateFutureCognition(FutureCognitionError):
    """A request identifier names one request permanently."""


class FutureCognitionTooSoon(FutureCognitionError):
    """The requested time is inside the minimum horizon.

    A mechanical anti-tight-loop bound, not a judgement about the request. It
    stops a turn spawning a turn without wall-clock time passing; it says
    nothing about whether the thought was worth having.
    """

    def __init__(self, minimum_seconds: int) -> None:
        self.minimum_seconds = minimum_seconds
        super().__init__(
            f"a future cognition must be at least {minimum_seconds} seconds ahead"
        )
