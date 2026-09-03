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

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

from alx.contracts.records import BackgroundEvent

if TYPE_CHECKING:
    from alx.contracts.cognition import CognitionOrigin
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


@dataclass(frozen=True, slots=True)
class CognitionOpportunity:
    """An occasion on which the authoritative Core is invoked.

    It asserts that something in AL/X's world is new, and it invokes her. It
    asserts nothing about importance, subject, urgency or what should be done,
    because deciding whether an occasion is worth pursuing is itself a
    judgement and only the Core makes it.

    The fields are exhaustive on purpose. There is no topic, summary,
    importance, priority, category, reason or suggested action: a field naming
    what an opportunity is *about* would be the runtime forming an opinion, and
    every rejected deterministic rule would eventually hide in it.

    `note` is present only for a self-requested occasion, carried verbatim from
    the matured request. It is AL/X's message to herself. Deterministic code
    transports it and never reads it.
    """

    opportunity_id: str
    origin: "CognitionOrigin"
    arose_at: datetime
    references: tuple[str, ...] = ()
    note: str | None = None
    provenance: "ContentProvenance | None" = None

    def __post_init__(self) -> None:
        from alx.contracts.cognition import CognitionOrigin as _Origin

        _required(self.opportunity_id, "opportunity_id")
        if not isinstance(self.origin, _Origin):
            raise TypeError("origin must be a CognitionOrigin")
        _aware(self.arose_at, "arose_at")
        if self.note is not None and not isinstance(self.note, str):
            raise TypeError("note must be a string or None")
        references = tuple(self.references)
        if any(not item.strip() for item in references):
            raise ValueError("references must not contain blanks")
        if len(references) != len(set(references)):
            raise ValueError("references must not contain duplicates")
        object.__setattr__(self, "references", references)


class CognitionOpportunitySource(Protocol):
    """The one way anything in the world reaches the Core.

    Mail, completed work and AL/X's own matured requests all implement this.
    There is deliberately one protocol rather than one per origin: a second
    ingress would be a second production path under Law 0, and the second one
    is always where a filter on interest eventually appears.
    """

    def events(self) -> AsyncIterator[BackgroundEvent]: ...

    def record_delivery(self, event_id: str) -> None: ...


class CarriedThoughtStatus(str, Enum):
    """Whether AL/X still holds a thought. Never how much it matters."""

    OPEN = "open"
    RAISED = "raised"
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True, slots=True)
class CarriedThought:
    """Something AL/X keeps on her mind, in her own words.

    It is content without an occasion: neither work with success criteria (a
    goal), nor a claim about the world (a notebook entry), nor a judgement that
    something mattered to her development (a memory), nor a moment she wants (a
    future cognition request). Without this it would simply evaporate.

    The fields are exhaustive on purpose. There is no priority, urgency,
    category, sentiment, importance, expiry, delivery time, score or topic,
    because each would be a place for a rule about when to raise a thought to
    hide, and this is durable unfinished thinking rather than a message queue.

    `content` is hers, stored and returned verbatim. Nothing in the runtime
    reads it, and the three status transitions are things she does, never
    things inferred from what she wrote.
    """

    thought_id: str
    content: str
    formed_at: datetime
    references: tuple[str, ...] = ()
    status: CarriedThoughtStatus = CarriedThoughtStatus.OPEN
    provenance: "ContentProvenance | None" = None

    def __post_init__(self) -> None:
        _required(self.thought_id, "thought_id")
        if not isinstance(self.content, str):
            raise TypeError("content must be a string")
        _aware(self.formed_at, "formed_at")
        if not isinstance(self.status, CarriedThoughtStatus):
            raise TypeError("status must be a CarriedThoughtStatus")
        references = tuple(self.references)
        if any(not item.strip() for item in references):
            raise ValueError("references must not contain blanks")
        if len(references) != len(set(references)):
            raise ValueError("references must not contain duplicates")
        object.__setattr__(self, "references", references)


class CarriedThoughtNotFound(FutureCognitionError):
    """No thought with that exact identity is in that state."""


class DuplicateCarriedThought(FutureCognitionError):
    """A thought identifier names one thought permanently."""


class AutonomousSpendAuthority(Protocol):
    """Authorises one autonomous Core call and settles what it cost.

    Stated here as a promise rather than imported from observability, because
    `core` depends only on `contracts`. Bootstrap binds the real ledger to it.

    `reserve` withdraws the worst case or raises; it never returns a smaller
    allowance, a cheaper model or a shorter bound, because a ceiling that
    quietly buys something lesser is not a ceiling.
    """

    def reserve(self, max_input_tokens: int, max_output_tokens: int) -> Any: ...

    def settle(self, reservation: Any, usage: Any) -> float: ...
