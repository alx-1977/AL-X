"""Records for work AL/X started elsewhere and is waiting on.

An external task is something handed to a service that finishes in its own
time: a review today, something else later. This tracks that it is outstanding
and that a result has appeared. It does not know what the task was for, what
the result says, or whether anything should be done about it.

The states are deliberately few, and deliberately not a progress bar. A state
is claimed only where something observable supports it. Nothing here reports
"running", because a service that has been asked and has not answered looks
exactly like one that is thinking, and asserting the difference would be
invention rather than observation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


def _required(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be blank")


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


class TaskState(str, Enum):
    """Where an external task stands, as far as anything can be observed.

    `REQUESTED` and `WAITING_FOR_RESULT` differ only in whether anything has
    been checked since. Neither claims the service is working, because nothing
    visible distinguishes a service that is thinking from one that has not
    started.

    There is no state for "found problems" or "looks fine". What a result says
    is a judgement, and it belongs to AL/X rather than to a status field.
    """

    REQUESTED = "requested"
    WAITING_FOR_RESULT = "waiting_for_result"
    COMPLETED = "completed"
    FAILED = "failed"
    STATUS_UNKNOWN = "status_unknown"
    OBSERVER_UNAVAILABLE = "observer_unavailable"

    @property
    def is_settled(self) -> bool:
        """Whether this task needs watching any longer."""
        return self in (
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.OBSERVER_UNAVAILABLE,
        )


@dataclass(frozen=True, slots=True)
class ExternalTask:
    """One outstanding piece of work, and where it stands.

    `subject_reference` names what the task is about in the external system's
    own terms — a pull request and a revision, for a review. It is opaque here:
    this module never parses it, and the observer that understands it is the
    one that produced it.
    """

    task_id: str
    kind: str
    service: str
    subject_reference: str
    state: TaskState
    requested_at: datetime
    last_checked_at: datetime | None = None
    conversation_id: str = ""
    # Set once, when a result is first seen. Kept so a completed task can be
    # handed to the Core without re-deriving what completed it.
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        _required(self.task_id, "task_id")
        _required(self.kind, "kind")
        _required(self.service, "service")
        _required(self.subject_reference, "subject_reference")
        if not isinstance(self.state, TaskState):
            raise TypeError("state must be a TaskState")
        _aware(self.requested_at, "requested_at")
        if self.last_checked_at is not None:
            _aware(self.last_checked_at, "last_checked_at")
        if self.completed_at is not None:
            _aware(self.completed_at, "completed_at")
        if self.state is TaskState.COMPLETED and self.completed_at is None:
            raise ValueError("a completed task records when it completed")
        if self.state is not TaskState.COMPLETED and self.completed_at is not None:
            raise ValueError("only a completed task records a completion time")

    def elapsed_seconds(self, at: datetime) -> float:
        """How long this has been outstanding, for display."""
        _aware(at, "at")
        end = self.completed_at or at
        return max(0.0, (end - self.requested_at).total_seconds())


@dataclass(frozen=True, slots=True)
class TaskObservation:
    """What one look at the external service found.

    An observer returns this and nothing else. It says whether a result now
    exists, never what the result contains: carrying content here would put
    the service's words on the status path, and reading them is the Core's
    work rather than the watcher's.
    """

    state: TaskState
    observed_at: datetime
    subject_reference: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.state, TaskState):
            raise TypeError("state must be a TaskState")
        _aware(self.observed_at, "observed_at")
        if self.subject_reference:
            _required(self.subject_reference, "subject_reference")


__all__ = [
    "ExternalTask",
    "TaskObservation",
    "TaskState",
]
