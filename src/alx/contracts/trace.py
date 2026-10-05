"""The operator's execution trace: what the runtime is doing, as it happens.

A trace event says which subsystem is active and what it is doing, in a fixed
operator vocabulary chosen by code. It is a technical log line, never AL/X's
voice: no field carries conversation content, capability arguments beyond
those a capability declares safe, results, or anything the model wrote. Her
reasoning stays private; what is exposed is the *purpose* of each reasoning
call, which the Core knows from its own state before it makes the call.

Emission is best effort by construction. A trace sink that fails must never
fail the work it describes, so `emit_trace` swallows every sink error.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any


# Bounds for the one free-form field. A label is a short fixed phrase, so a
# long or multi-line value means something other than a label reached it.
MAX_TRACE_LABEL_CHARACTERS = 80
MAX_TRACE_REFERENCE_CHARACTERS = 96


class TraceSubsystem(str, Enum):
    """Where the work is happening. Rendered as the line's column heading."""

    CORE = "core"
    PLAN = "plan"
    GOALS = "goals"
    MAIL = "mail"
    GIT = "git"
    GITHUB = "github"
    CODING = "coding"
    REVIEW = "review"
    RESEARCH = "research"
    WEB = "web"
    NOTEBOOK = "notebook"
    THOUGHTS = "thoughts"
    XERO = "xero"
    DHL = "dhl"
    SANDBOX = "sandbox"
    VOICE = "voice"
    CAPABILITY = "capability"


class TraceStatus(str, Enum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    REFUSED = "refused"
    WAITING = "waiting"
    INFO = "info"


class ReasoningPurpose(str, Enum):
    """Why the Core is about to make a reasoning call.

    Derived by deterministic code from the loop's own state: what started the
    turn and what changed since the previous step. It names the occasion for a
    call, never its content or outcome, so it reveals nothing she thinks.
    """

    INTERPRETING_REQUEST = "interpreting_request"
    ASSESSING_EVENT = "assessing_event"
    REVIEWING_COMPLETED_WORK = "reviewing_completed_work"
    REVISITING_FOLLOW_UP = "revisiting_follow_up"
    EVALUATING_PLAN = "evaluating_plan"
    EVALUATING_GOAL_STATE = "evaluating_goal_state"
    REVIEWING_RESULT = "reviewing_result"
    REVIEWING_MEMORIES = "reviewing_memories"
    RECONSIDERING_REFUSAL = "reconsidering_refusal"
    CORRECTING_DECISION = "correcting_decision"
    CONTINUING_WORK = "continuing_work"
    PREPARING_RESPONSE = "preparing_response"
    DECIDING_NEXT_STEP = "deciding_next_step"

    @property
    def label(self) -> str:
        return _PURPOSE_LABELS[self]


_PURPOSE_LABELS = {
    ReasoningPurpose.INTERPRETING_REQUEST: "Interpreting request",
    ReasoningPurpose.ASSESSING_EVENT: "Assessing external event",
    ReasoningPurpose.REVIEWING_COMPLETED_WORK: "Reviewing completed work",
    ReasoningPurpose.REVISITING_FOLLOW_UP: "Revisiting requested follow-up",
    ReasoningPurpose.EVALUATING_PLAN: "Evaluating plan progress",
    ReasoningPurpose.EVALUATING_GOAL_STATE: "Evaluating goal state",
    ReasoningPurpose.REVIEWING_RESULT: "Reviewing result",
    ReasoningPurpose.REVIEWING_MEMORIES: "Reviewing retrieved memories",
    ReasoningPurpose.RECONSIDERING_REFUSAL: "Reconsidering after refusal",
    ReasoningPurpose.CORRECTING_DECISION: "Correcting rejected decision",
    ReasoningPurpose.CONTINUING_WORK: "Continuing remaining work",
    ReasoningPurpose.PREPARING_RESPONSE: "Preparing response",
    ReasoningPurpose.DECIDING_NEXT_STEP: "Deciding next step",
}


@dataclass(frozen=True, slots=True)
class TraceEvent:
    """One operator-visible step. Content-free by construction."""

    subsystem: TraceSubsystem
    status: TraceStatus
    label: str
    # A structured identifier the operator can correlate: a capability id, a
    # short goal or plan reference, a purpose code. Never content.
    reference: str | None = None
    # A technical reason code for a refusal or failure.
    reason_code: str | None = None
    duration_ms: int | None = None
    # A small count, such as how many steps a plan holds.
    count: int | None = None
    # The conversation the work belongs to, when known. Used for routing only;
    # it is never placed in the rendered event.
    conversation_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.subsystem, TraceSubsystem):
            raise TypeError("subsystem must be a TraceSubsystem")
        if not isinstance(self.status, TraceStatus):
            raise TypeError("status must be a TraceStatus")
        _bounded(self.label, "label", MAX_TRACE_LABEL_CHARACTERS)
        for name in ("reference", "reason_code"):
            value = getattr(self, name)
            if value is not None:
                _bounded(value, name, MAX_TRACE_REFERENCE_CHARACTERS)
        for name in ("duration_ms", "count"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer")

    def values(self) -> dict[str, Any]:
        """The diagnostic payload a console renders. No routing fields."""
        rendered: dict[str, Any] = {
            "code": "trace",
            "subsystem": self.subsystem.value,
            "status": self.status.value,
            "label": self.label,
        }
        for name in ("reference", "reason_code", "duration_ms", "count"):
            value = getattr(self, name)
            if value is not None:
                rendered[name] = value
        return rendered


TraceSink = Callable[[TraceEvent], None]


def emit_trace(
    sink: TraceSink | None,
    subsystem: TraceSubsystem,
    status: TraceStatus,
    label: str,
    **values: Any,
) -> None:
    """Build and deliver one event, or nothing.

    Construction happens inside the guard too: a value that fails validation
    loses one trace line, never the work it describes.
    """
    if sink is None:
        return
    try:
        sink(TraceEvent(subsystem, status, label, **values))
    except Exception:  # noqa: BLE001 - observability must not alter execution
        return


def _bounded(value: str, name: str, limit: int) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank")
    if len(value) > limit or "\n" in value or "\r" in value:
        raise ValueError(f"{name} must be one short line")
