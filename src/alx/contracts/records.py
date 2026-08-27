"""Small immutable records shared by the AL/X foundation boundaries.

These records deliberately describe state and proposed capability work only.
They do not choose a capability, interpret a conversation turn, store data, or
decide a response.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Mapping, TypeAlias


StructuredValue: TypeAlias = None | bool | int | float | str | tuple["StructuredValue", ...] | Mapping[str, "StructuredValue"]
StructuredData: TypeAlias = Mapping[str, StructuredValue]


def _required(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")


def _aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _references(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    frozen = tuple(values)
    if any(not value.strip() for value in frozen):
        raise ValueError(f"{field_name} must not contain blank references")
    return frozen


def _freeze_value(value: StructuredValue) -> StructuredValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, StructuredValue] = {}
        for key, nested in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError("structured data keys must be non-empty strings")
            frozen[key] = _freeze_value(nested)
        return MappingProxyType(frozen)
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_value(nested) for nested in value)
    raise TypeError("structured data may contain only scalar values, mappings, and sequences")


def freeze_data(data: StructuredData) -> StructuredData:
    """Return a deeply immutable structured mapping suitable for a boundary."""
    frozen = _freeze_value(data)
    if not isinstance(frozen, Mapping):
        raise TypeError("structured data must be a mapping")
    return frozen


class ConversationOrigin(str, Enum):
    TYPED = "typed"
    SPEECH_TRANSCRIPT = "speech_transcript"


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """The sole contract allowed to hold an unmodified conversational utterance."""

    conversation_id: str
    turn_id: str
    origin: ConversationOrigin
    content: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        _required(self.conversation_id, "conversation_id")
        _required(self.turn_id, "turn_id")
        _required(self.content, "content")
        _aware(self.occurred_at, "occurred_at")


@dataclass(frozen=True, slots=True)
class BackgroundEvent:
    """A structured fact for the gateway; it has no conversational authority."""

    event_id: str
    kind: str
    occurred_at: datetime
    data: StructuredData = field(default_factory=dict)

    def __post_init__(self) -> None:
        _required(self.event_id, "event_id")
        _required(self.kind, "kind")
        object.__setattr__(self, "data", freeze_data(self.data))
        _aware(self.occurred_at, "occurred_at")


@dataclass(frozen=True, slots=True)
class Objective:
    source_reference: str
    summary: str

    def __post_init__(self) -> None:
        _required(self.source_reference, "source_reference")
        _required(self.summary, "summary")


@dataclass(frozen=True, slots=True)
class SuccessCriterion:
    criterion_id: str
    description: str

    def __post_init__(self) -> None:
        _required(self.criterion_id, "criterion_id")
        _required(self.description, "description")


@dataclass(frozen=True, slots=True)
class Referent:
    referent_id: str
    attributes: StructuredData = field(default_factory=dict)

    def __post_init__(self) -> None:
        _required(self.referent_id, "referent_id")
        object.__setattr__(self, "attributes", freeze_data(self.attributes))


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    kind: str
    attributes: StructuredData = field(default_factory=dict)
    supports: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required(self.evidence_id, "evidence_id")
        _required(self.kind, "kind")
        object.__setattr__(self, "supports", _references(self.supports, "evidence support references"))
        object.__setattr__(self, "attributes", freeze_data(self.attributes))


@dataclass(frozen=True, slots=True)
class ProgressRecord:
    record_id: str
    summary: str
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required(self.record_id, "record_id")
        _required(self.summary, "summary")
        object.__setattr__(self, "evidence_refs", _references(self.evidence_refs, "evidence references"))


@dataclass(frozen=True, slots=True)
class WorkItem:
    item_id: str
    summary: str

    def __post_init__(self) -> None:
        _required(self.item_id, "item_id")
        _required(self.summary, "summary")


class CapabilityResultState(str, Enum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class CapabilityAttemptDisposition(str, Enum):
    PENDING = "pending"
    EXECUTED = "executed"
    REJECTED = "rejected"
    BROKER_FAILURE = "broker_failure"
    LEGACY = "legacy"


@dataclass(frozen=True, slots=True)
class CapabilityCall:
    """A language-blind proposal; it contains structured arguments, never a turn."""

    call_id: str
    capability_id: str
    arguments: StructuredData = field(default_factory=dict)
    approval_id: str | None = None

    def __post_init__(self) -> None:
        _required(self.call_id, "call_id")
        _required(self.capability_id, "capability_id")
        if self.approval_id is not None:
            _required(self.approval_id, "approval_id")
        object.__setattr__(self, "arguments", freeze_data(self.arguments))


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    call_id: str
    capability_id: str
    state: CapabilityResultState
    values: StructuredData = field(default_factory=dict)
    failure: StructuredData | None = None
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required(self.call_id, "call_id")
        _required(self.capability_id, "capability_id")
        object.__setattr__(self, "evidence_refs", _references(self.evidence_refs, "evidence references"))
        object.__setattr__(self, "values", freeze_data(self.values))
        if self.failure is not None:
            object.__setattr__(self, "failure", freeze_data(self.failure))
        if self.state is CapabilityResultState.SUCCEEDED and self.failure is not None:
            raise ValueError("a succeeded result cannot include failure details")
        if self.state is CapabilityResultState.FAILED and self.failure is None:
            raise ValueError("a failed result requires structured failure details")
        if self.state is CapabilityResultState.PARTIAL and not self.values:
            raise ValueError("a partial result requires available structured values")


@dataclass(frozen=True, slots=True)
class CapabilityAttempt:
    call: CapabilityCall | None
    disposition: CapabilityAttemptDisposition
    implementation_invoked: bool | None
    result: CapabilityResult | None = None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, CapabilityAttemptDisposition):
            raise TypeError("attempt disposition must be a CapabilityAttemptDisposition")
        if self.reason_code is not None:
            _required(self.reason_code, "reason_code")
        if self.call is not None and self.result is not None and (
            self.result.call_id != self.call.call_id or self.result.capability_id != self.call.capability_id
        ):
            raise ValueError("attempt result must identify its original call")
        if self.disposition is CapabilityAttemptDisposition.LEGACY:
            if self.call is not None or self.implementation_invoked is not None or self.result is None or self.reason_code != "legacy_v1":
                raise ValueError("legacy attempts retain only explicitly recoverable v1 results")
        elif self.call is None:
            raise ValueError("non-legacy attempts require an original call")
        elif self.disposition is CapabilityAttemptDisposition.PENDING:
            if self.implementation_invoked is not None or self.result is not None or self.reason_code != "dispatch_pending":
                raise ValueError("pending attempts record unresolved dispatch without claiming an invocation")
        elif not isinstance(self.implementation_invoked, bool):
            raise TypeError("non-legacy attempts require a boolean invocation flag")
        elif self.disposition is CapabilityAttemptDisposition.EXECUTED:
            if not self.implementation_invoked or self.result is None:
                raise ValueError("executed attempts require an invoked implementation and result")
        elif self.disposition is CapabilityAttemptDisposition.REJECTED:
            if self.implementation_invoked or self.result is not None or self.reason_code is None:
                raise ValueError("rejected attempts require a reason without invocation or result")
        elif self.disposition is CapabilityAttemptDisposition.BROKER_FAILURE:
            if self.result is None or self.result.state is not CapabilityResultState.FAILED or self.reason_code is None:
                raise ValueError("broker failures require a result and reason")

    @property
    def state(self) -> CapabilityAttemptDisposition:
        return self.disposition

    @property
    def reason(self) -> str | None:
        return self.reason_code


class ApprovalLifecycle(str, Enum):
    REQUESTED = "requested"
    GRANTED = "granted"
    CLAIMED = "claimed"
    DENIED = "denied"
    WITHDRAWN = "withdrawn"
    EXPIRED = "expired"
    CONSUMED = "consumed"


@dataclass(frozen=True, slots=True)
class ApprovalScope:
    """Exact structured action scope that an approval can authorize."""

    capability_id: str
    arguments: StructuredData

    def __post_init__(self) -> None:
        _required(self.capability_id, "capability_id")
        object.__setattr__(self, "arguments", freeze_data(self.arguments))

    def matches(self, call: CapabilityCall) -> bool:
        return self.capability_id == call.capability_id and self.arguments == call.arguments


@dataclass(frozen=True, slots=True)
class Approval:
    approval_id: str
    scope: ApprovalScope
    lifecycle: ApprovalLifecycle
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        _required(self.approval_id, "approval_id")
        if self.expires_at is not None:
            _aware(self.expires_at, "expires_at")
        if self.lifecycle is ApprovalLifecycle.EXPIRED and self.expires_at is None:
            raise ValueError("an expired approval requires an expiry time")

    def permits(self, call: CapabilityCall, at: datetime) -> bool:
        _aware(at, "at")
        return (
            self.lifecycle is ApprovalLifecycle.GRANTED
            and call.approval_id == self.approval_id
            and (self.expires_at is None or at <= self.expires_at)
            and self.scope.matches(call)
        )


class GoalStatus(str, Enum):
    ACTIVE = "active"
    AWAITING_INPUT = "awaiting_input"
    AWAITING_APPROVAL = "awaiting_approval"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class GoalStopReason(str, Enum):
    SUCCESS_CRITERIA_MET = "success_criteria_met"
    GENUINELY_BLOCKED = "genuinely_blocked"
    REQUIRED_INPUT = "required_input"
    REQUIRED_APPROVAL = "required_approval"
    CANCELLED = "cancelled"


_STOP_BY_STATUS = {
    GoalStatus.AWAITING_INPUT: GoalStopReason.REQUIRED_INPUT,
    GoalStatus.AWAITING_APPROVAL: GoalStopReason.REQUIRED_APPROVAL,
    GoalStatus.BLOCKED: GoalStopReason.GENUINELY_BLOCKED,
    GoalStatus.COMPLETED: GoalStopReason.SUCCESS_CRITERIA_MET,
    GoalStatus.CANCELLED: GoalStopReason.CANCELLED,
}


@dataclass(frozen=True, slots=True)
class GoalState:
    """Durable goal state. A storage boundary, not this contract, persists it."""

    goal_id: str
    objective: Objective
    success_criteria: tuple[SuccessCriterion, ...]
    context: StructuredData = field(default_factory=dict)
    referents: tuple[Referent, ...] = ()
    decisions: tuple[ProgressRecord, ...] = ()
    corrections: tuple[ProgressRecord, ...] = ()
    progress: tuple[ProgressRecord, ...] = ()
    attempts: tuple[CapabilityAttempt, ...] = ()
    blockers: tuple[WorkItem, ...] = ()
    outstanding_work: tuple[WorkItem, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    approvals: tuple[Approval, ...] = ()
    status: GoalStatus = GoalStatus.ACTIVE
    stop_reason: GoalStopReason | None = None

    def __post_init__(self) -> None:
        _required(self.goal_id, "goal_id")
        if not self.success_criteria:
            raise ValueError("a goal requires at least one success criterion")
        object.__setattr__(self, "context", freeze_data(self.context))
        for name in (
            "success_criteria", "referents", "decisions", "corrections", "progress",
            "attempts", "blockers", "outstanding_work", "evidence", "approvals",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        pending = tuple(
            item
            for item in self.attempts
            if item.disposition is CapabilityAttemptDisposition.PENDING
        )
        if len(pending) > 1:
            raise ValueError("a goal may contain only one unresolved dispatch")
        if pending and pending[0] != self.attempts[-1]:
            raise ValueError("an unresolved dispatch must be the latest attempt")
        if pending and self.status is not GoalStatus.ACTIVE:
            raise ValueError("an unresolved dispatch requires an active goal")
        for approval in self.approvals:
            if approval.lifecycle is ApprovalLifecycle.CLAIMED and not any(
                item.call is not None
                and item.call.approval_id == approval.approval_id
                and approval.scope.matches(item.call)
                for item in pending
            ):
                raise ValueError("a claimed approval requires its pending capability attempt")
        expected = _STOP_BY_STATUS.get(self.status)
        if self.status is GoalStatus.ACTIVE:
            if self.stop_reason is not None:
                raise ValueError("an active goal cannot have a stop reason")
            return
        if self.stop_reason is not expected:
            raise ValueError("goal status requires its legitimate stop reason")
        if self.status is GoalStatus.COMPLETED:
            if self.blockers or self.outstanding_work:
                raise ValueError("a completed goal cannot retain blockers or outstanding work")
            supported = {reference for item in self.evidence for reference in item.supports}
            missing = {item.criterion_id for item in self.success_criteria} - supported
            if missing:
                raise ValueError("a completed goal requires evidence for every success criterion")
        if self.status is GoalStatus.BLOCKED and not self.blockers:
            raise ValueError("a blocked goal requires at least one blocker")
        if self.status is GoalStatus.AWAITING_INPUT and not self.outstanding_work:
            raise ValueError("awaiting input requires outstanding work")
        if self.status is GoalStatus.AWAITING_APPROVAL and not any(
            item.lifecycle is ApprovalLifecycle.REQUESTED for item in self.approvals
        ):
            raise ValueError("awaiting approval requires a requested approval record")

    @property
    def continues(self) -> bool:
        return self.status is GoalStatus.ACTIVE

    @property
    def completed_actions(self) -> tuple[CapabilityResult, ...]:
        return tuple(item.result for item in self.attempts if item.result is not None and item.disposition in (CapabilityAttemptDisposition.EXECUTED, CapabilityAttemptDisposition.LEGACY))
