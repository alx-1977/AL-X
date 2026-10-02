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
from typing import Mapping, TypeAlias, TYPE_CHECKING

if TYPE_CHECKING:
    from alx.contracts.provenance import ContentProvenance


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
    ALX_RESPONSE = "alx_response"


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """The sole contract allowed to hold an unmodified conversational utterance."""

    conversation_id: str
    turn_id: str
    origin: ConversationOrigin
    content: str
    occurred_at: datetime
    person_id: str | None = None
    provenance: ContentProvenance | None = None

    def __post_init__(self) -> None:
        _required(self.conversation_id, "conversation_id")
        _required(self.turn_id, "turn_id")
        _required(self.content, "content")
        _aware(self.occurred_at, "occurred_at")
        if self.person_id is not None:
            _required(self.person_id, "person_id")
        if self.provenance is not None:
            from alx.contracts.provenance import ContentProvenance

            if not isinstance(self.provenance, ContentProvenance):
                raise TypeError("turn provenance must be ContentProvenance or None")


@dataclass(frozen=True, slots=True)
class BackgroundEvent:
    """A structured fact for the gateway; it has no conversational authority."""

    event_id: str
    kind: str
    occurred_at: datetime
    data: StructuredData = field(default_factory=dict)
    transient_data: StructuredData = field(default_factory=dict)
    provenance: ContentProvenance | None = None

    def __post_init__(self) -> None:
        _required(self.event_id, "event_id")
        _required(self.kind, "kind")
        object.__setattr__(self, "data", freeze_data(self.data))
        object.__setattr__(self, "transient_data", freeze_data(self.transient_data))
        _aware(self.occurred_at, "occurred_at")
        if self.provenance is not None:
            from alx.contracts.provenance import ContentProvenance

            if not isinstance(self.provenance, ContentProvenance):
                raise TypeError("event provenance must be ContentProvenance or None")

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
    source_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required(self.evidence_id, "evidence_id")
        _required(self.kind, "kind")
        object.__setattr__(self, "supports", _references(self.supports, "evidence support references"))
        object.__setattr__(
            self,
            "source_references",
            _references(self.source_references, "evidence source references"),
        )
        object.__setattr__(self, "attributes", freeze_data(self.attributes))


def history_evidence_ids(*groups: tuple[Evidence, ...]) -> frozenset[str]:
    """The bare evidence identifiers history records may cite."""
    return frozenset(item.evidence_id for group in groups for item in group)


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


class ExecutionOutcome(str, Enum):
    """What one result means for work already decided, said by its capability.

    Settlement, never permission: SUCCESS says the operation finished as
    asked, not that anything may follow it. Only a plan observation may report
    PENDING or TEMPORARILY_UNAVAILABLE. A result that names none has the
    outcome its state implies (see `outcome_of`).
    """

    SUCCESS = "success"
    PENDING = "pending"
    FAILURE = "failure"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    AMBIGUOUS = "ambiguous"


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
    durable_arguments: StructuredData | None = None

    def __post_init__(self) -> None:
        _required(self.call_id, "call_id")
        _required(self.capability_id, "capability_id")
        if self.approval_id is not None:
            _required(self.approval_id, "approval_id")
        object.__setattr__(self, "arguments", freeze_data(self.arguments))
        durable = self.arguments if self.durable_arguments is None else self.durable_arguments
        durable = freeze_data(durable)
        if any(
            key not in self.arguments or self.arguments[key] != value
            for key, value in durable.items()
        ):
            raise ValueError("durable arguments must be an exact input projection")
        object.__setattr__(self, "durable_arguments", durable)


@dataclass(frozen=True, slots=True)
class PlanCondition:
    """An exact, model-chosen completion check on one structured result field."""

    path: str
    equals: StructuredData | str | int | float | bool | None
    negate: bool = False

    def __post_init__(self) -> None:
        _required(self.path, "condition path")
        if any(not part or part.startswith("_") or part == "*"
               for part in self.path.split(".")):
            raise ValueError("condition paths must name public structured fields")
        if not isinstance(self.negate, bool):
            raise TypeError("negate must be a bool")
        object.__setattr__(self, "equals", _freeze_value(self.equals))


# The longest a plan may keep observing one pending step before AL/X is told.
MAX_PLAN_WAIT_SECONDS = 86_400


@dataclass(frozen=True, slots=True)
class ExecutionStep:
    """One exact capability call AL/X decided, and how long it may wait."""

    call: CapabilityCall
    # Checked only on a SUCCESS outcome. Empty means SUCCESS alone completes.
    completion_conditions: tuple[PlanCondition, ...] = ()
    # Zero: the step never waits. Otherwise a pending observation is repeated
    # every `wait_seconds` until `max_wait_seconds` have passed.
    wait_seconds: int = 0
    max_wait_seconds: int = 0
    # Advance, then return to AL/X with this step's result.
    wake_core_on_completion: bool = False
    waiting_for: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "completion_conditions", tuple(self.completion_conditions))
        if self.call.arguments != self.call.durable_arguments:
            raise ValueError("planned arguments must be safe for durable storage")
        for name in ("wait_seconds", "max_wait_seconds"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.wait_seconds and not (
            self.wait_seconds <= self.max_wait_seconds <= MAX_PLAN_WAIT_SECONDS
        ):
            raise ValueError("a waiting step needs a bound between its interval and a day")
        if not self.wait_seconds and self.max_wait_seconds:
            raise ValueError("only a waiting step has a wait bound")
        if not isinstance(self.wake_core_on_completion, bool):
            raise TypeError("wake_core_on_completion must be a bool")
        if self.waiting_for is not None:
            _required(self.waiting_for, "waiting_for")


class PlanStatus(str, Enum):
    RUNNING = "running"
    WAITING = "waiting"
    NEEDS_CORE = "needs_core"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


PLAN_TERMINAL = frozenset({PlanStatus.COMPLETED, PlanStatus.CANCELLED})


@dataclass(frozen=True, slots=True)
class PlanDispatch:
    """The one planned dispatch checkpointed and not yet reduced.

    A result belongs to the plan only when its call carries this call_id,
    which the runner generates for each dispatch and writes here first.
    """

    step_index: int
    call_id: str

    def __post_init__(self) -> None:
        _required(self.call_id, "call_id")
        if not isinstance(self.step_index, int) or self.step_index < 0:
            raise ValueError("step_index must be a nonnegative integer")


@dataclass(frozen=True, slots=True)
class PlanAttention:
    """Why a plan needs AL/X, and how often it has been offered to her.

    The plan owns this: nothing else records whether she is needed. It is
    cleared only by her resolving this exact `seq`.
    """

    seq: int
    reason: str
    facts: tuple[str, ...]
    raised_at: datetime
    # The plan's own dispatches whose results she is to judge.
    evidence_call_ids: tuple[str, ...] = ()
    # Automatic offers made, and how many of them may have reached a paid
    # reasoning call. Exhaustion is counted from paid offers alone.
    offers: int = 0
    paid_offers: int = 0
    next_offer_at: datetime | None = None
    # No further automatic offer. Still owned and still shown to her.
    blocked: bool = False

    def __post_init__(self) -> None:
        _required(self.reason, "attention reason")
        object.__setattr__(self, "facts", tuple(self.facts))
        object.__setattr__(self, "evidence_call_ids", tuple(self.evidence_call_ids))
        if not self.facts or self.facts[0] != self.reason:
            raise ValueError("the first attention fact is its reason")
        for item in (*self.facts, *self.evidence_call_ids):
            _required(item, "attention fact")
        _aware(self.raised_at, "raised_at")
        if self.next_offer_at is not None:
            _aware(self.next_offer_at, "next_offer_at")
        for name in ("seq", "offers", "paid_offers"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.seq < 1 or self.paid_offers > self.offers:
            raise ValueError("attention counters are inconsistent")
        if not isinstance(self.blocked, bool):
            raise TypeError("blocked must be a bool")


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """AL/X's durable intent, with only its mechanical cursor advanced by code."""

    plan_id: str
    objective_source: str | None
    objective_summary: str | None
    source_turn_id: str | None
    steps: tuple[ExecutionStep, ...]
    context_preconditions: StructuredData = field(default_factory=dict)
    cursor: int = 0
    status: PlanStatus = PlanStatus.RUNNING
    next_due_at: datetime | None = None
    # When a waiting step stops waiting and returns to her.
    wait_deadline: datetime | None = None
    inflight: PlanDispatch | None = None
    # Counts every attention this plan has raised; the current one has it.
    attention_seq: int = 0
    attention: PlanAttention | None = None

    def __post_init__(self) -> None:
        _required(self.plan_id, "plan_id")
        # The objective a plan serves is the goal's, bound by the Core when it
        # installs the plan. A proposal arriving from reasoning carries none.
        for name in ("objective_source", "objective_summary"):
            if getattr(self, name) is not None:
                _required(getattr(self, name), name)
        object.__setattr__(self, "steps", tuple(self.steps))
        object.__setattr__(self, "context_preconditions", freeze_data(self.context_preconditions))
        object.__setattr__(self, "status", PlanStatus(self.status))
        if not self.steps or len(self.steps) > 32 or not 0 <= self.cursor <= len(self.steps):
            raise ValueError("plan requires steps and an in-range cursor")
        if len({step.call.call_id for step in self.steps}) != len(self.steps):
            raise ValueError("plan call identifiers must be unique")
        for name in ("next_due_at", "wait_deadline"):
            if getattr(self, name) is not None:
                _aware(getattr(self, name), name)
        executable = self.status in {PlanStatus.RUNNING, PlanStatus.WAITING}
        if executable and self.cursor == len(self.steps):
            raise ValueError("an executable plan needs a step at its cursor")
        if (self.status is PlanStatus.WAITING) != (self.next_due_at is not None):
            raise ValueError("exactly a waiting plan has a due time")
        if self.wait_deadline is not None and not executable:
            raise ValueError("only an executable plan has a wait deadline")
        if self.inflight is not None and (
            self.status is not PlanStatus.RUNNING or self.inflight.step_index != self.cursor
        ):
            raise ValueError("only a running plan has a dispatch in flight, at its cursor")
        if (self.status is PlanStatus.NEEDS_CORE) != (self.attention is not None):
            raise ValueError("exactly a plan that needs AL/X carries an attention")
        if (not isinstance(self.attention_seq, int) or isinstance(self.attention_seq, bool)
                or self.attention_seq < 0):
            raise ValueError("attention_seq must be a nonnegative integer")
        if self.attention is not None and self.attention.seq != self.attention_seq:
            raise ValueError("the current attention carries the plan's attention_seq")


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    call_id: str
    capability_id: str
    state: CapabilityResultState
    values: StructuredData = field(default_factory=dict)
    failure: StructuredData | None = None
    evidence_refs: tuple[str, ...] = ()
    durable_values: StructuredData | None = None
    provenance: ContentProvenance | None = None
    # Set by a capability that knows more than its state says: still pending,
    # temporarily unavailable, needing AL/X's judgment, or a read that
    # succeeded in observing work that failed. None means the outcome its
    # state implies.
    outcome: ExecutionOutcome | None = None

    def __post_init__(self) -> None:
        _required(self.call_id, "call_id")
        _required(self.capability_id, "capability_id")
        if self.outcome is not None:
            object.__setattr__(self, "outcome", ExecutionOutcome(self.outcome))
            if (self.outcome is ExecutionOutcome.SUCCESS
                    and self.state is not CapabilityResultState.SUCCEEDED):
                raise ValueError("only a succeeded result can report success")
        object.__setattr__(self, "evidence_refs", _references(self.evidence_refs, "evidence references"))
        object.__setattr__(self, "values", freeze_data(self.values))
        durable_values = self.values if self.durable_values is None else self.durable_values
        object.__setattr__(self, "durable_values", freeze_data(durable_values))
        if self.failure is not None:
            object.__setattr__(self, "failure", freeze_data(self.failure))
        if self.state is CapabilityResultState.SUCCEEDED and self.failure is not None:
            raise ValueError("a succeeded result cannot include failure details")
        if self.state is CapabilityResultState.FAILED and self.failure is None:
            raise ValueError("a failed result requires structured failure details")
        if self.state is CapabilityResultState.PARTIAL and not self.values:
            raise ValueError("a partial result requires available structured values")
        if self.provenance is not None:
            from alx.contracts.provenance import ContentProvenance

            if not isinstance(self.provenance, ContentProvenance):
                raise TypeError("result provenance must be ContentProvenance or None")


def outcome_of(result: CapabilityResult) -> ExecutionOutcome:
    """The outcome a result reports, or the one its state implies.

    A failure the capability marks as needing judgment is AMBIGUOUS, and a
    partial result always is: only a whole success or a settled failure is
    known without her.
    """
    if result.outcome is not None:
        return result.outcome
    if result.state is CapabilityResultState.SUCCEEDED:
        return ExecutionOutcome.SUCCESS
    if result.state is CapabilityResultState.FAILED and not (
        result.failure or {}
    ).get("requires_judgement"):
        return ExecutionOutcome.FAILURE
    return ExecutionOutcome.AMBIGUOUS


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


@dataclass(frozen=True, slots=True)
class ApprovalProposal:
    """An exact action approval grounded in the current person's durable turn."""

    approval_id: str
    scope: ApprovalScope
    source_reference: str

    def __post_init__(self) -> None:
        _required(self.approval_id, "approval_id")
        _required(self.source_reference, "source_reference")


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


class GoalMutationKind(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    AWAIT_INPUT = "await_input"
    AWAIT_APPROVAL = "await_approval"
    BLOCK = "block"
    CANCEL = "cancel"
    REQUEST_COMPLETION = "request_completion"


@dataclass(frozen=True, slots=True)
class GoalProposal:
    """A model-authored suggestion; only the Core may reduce it into goal truth."""

    kind: GoalMutationKind
    objective_summary: str | None = None
    success_criteria: tuple[SuccessCriterion, ...] | None = None
    context: StructuredData | None = None
    referents: tuple[Referent, ...] | None = None
    new_decisions: tuple[ProgressRecord, ...] = ()
    new_corrections: tuple[ProgressRecord, ...] = ()
    new_progress: tuple[ProgressRecord, ...] = ()
    blockers: tuple[WorkItem, ...] | None = None
    outstanding_work: tuple[WorkItem, ...] | None = None
    new_evidence: tuple[Evidence, ...] = ()

    def __post_init__(self) -> None:
        if self.objective_summary is not None:
            _required(self.objective_summary, "objective_summary")
        if self.success_criteria is not None:
            object.__setattr__(self, "success_criteria", tuple(self.success_criteria))
        if self.context is not None:
            object.__setattr__(self, "context", freeze_data(self.context))
        for name in (
            "referents",
            "blockers",
            "outstanding_work",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, tuple(value))
        for name in (
            "new_decisions",
            "new_corrections",
            "new_progress",
            "new_evidence",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))


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
    execution_plan: ExecutionPlan | None = None

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
        if self.execution_plan is not None and (
            self.execution_plan.objective_source is None
            or self.execution_plan.objective_summary is None
        ):
            raise ValueError("a goal's plan must be bound to its objective")
        inflight = None if self.execution_plan is None else self.execution_plan.inflight
        if inflight is not None and not any(
            item.call is not None and item.call.call_id == inflight.call_id
            for item in self.attempts
        ):
            raise ValueError("a plan's dispatch in flight must be a recorded attempt")
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
            supported = {
                reference
                for item in self.evidence
                if item.source_references
                for reference in item.supports
            }
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
