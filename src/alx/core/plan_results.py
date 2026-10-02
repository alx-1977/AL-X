"""The one interpretation of a planned capability result, and its one reduction.

D-036 lets deterministic code advance a plan AL/X already decided. The whole
lifecycle fits here:

    RUNNING  --SUCCESS + completion-->  RUNNING (next step)
    RUNNING  --PENDING / UNAVAILABLE--> WAITING --due--> RUNNING
    RUNNING / WAITING --anything else--> NEEDS_CORE (attention seq + 1)
    NEEDS_CORE --her resume / complete / cancel--> RUNNING / COMPLETED / CANCELLED

A result belongs to the plan only when its call id is the plan's `inflight`
dispatch; the caller proves that before classifying. Every such result is
classified exactly once, here, by a fixed precedence:

1. the dispatch was interrupted with no result;
2. the call was refused;
3. the capability's outcome: FAILURE and AMBIGUOUS wake her, PENDING and
   TEMPORARILY_UNAVAILABLE wait while the step allows it, SUCCESS advances
   only when the declared completion conditions hold.

Before each dispatch, `plan_invalidation_facts` checks what the plan
declared it depends on; a change there wakes her instead of dispatching.

Nothing here reads the shape of a condition to guess at failure. Whether a
result is pending, failed or ambiguous is the capability's statement.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from alx.contracts import (
    ApprovalLifecycle,
    CapabilityAttempt,
    CapabilityAttemptDisposition,
    CapabilityDefinition,
    ExecutionOutcome,
    ExecutionPlan,
    ExecutionStep,
    GoalState,
    GoalStatus,
    PlanAttention,
    PlanCondition,
    PlanStatus,
    outcome_of,
)


class PlanResultKind(str, Enum):
    ADVANCE = "advance"
    WAIT = "wait"
    WAKE_CORE = "wake_core"


@dataclass(frozen=True, slots=True)
class PlanResultClassification:
    """What one planned result means for the plan, and every fact behind it."""

    kind: PlanResultKind
    facts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "facts", tuple(dict.fromkeys(self.facts)))
        if self.kind is PlanResultKind.WAKE_CORE and not self.facts:
            raise ValueError("a Core wake must name what woke her")
        if self.kind is not PlanResultKind.WAKE_CORE and self.facts:
            raise ValueError("only a Core wake carries facts")


def plan_invalidation_facts(
    plan: ExecutionPlan, state: GoalState, now: datetime,
) -> tuple[str, ...]:
    """Rule 1: which declared precondition of the next step no longer holds.

    Only what the plan declared, or what any dispatch needs: an active goal,
    the objective it was bound to, its context preconditions, and a valid
    approval for the step about to run. Nothing else about the goal or the
    conversation touches a plan.
    """
    facts = []
    if state.status is not GoalStatus.ACTIVE:
        facts.append("goal_inactive")
    if (state.objective.source_reference != plan.objective_source
            or state.objective.summary != plan.objective_summary
            or any(key not in state.context or not json_equal(state.context[key], value)
                   for key, value in plan.context_preconditions.items())):
        facts.append("plan_precondition_changed")
    if plan.cursor < len(plan.steps):
        approval_id = plan.steps[plan.cursor].call.approval_id
        if approval_id is not None:
            approval = next(
                (item for item in state.approvals if item.approval_id == approval_id), None,
            )
            if (approval is None or approval.lifecycle is not ApprovalLifecycle.GRANTED
                    or not approval.scope.matches(plan.steps[plan.cursor].call)
                    or (approval.expires_at is not None and approval.expires_at <= now)):
                facts.append("plan_approval_invalid")
    return tuple(facts)


def classify_planned_result(
    step: ExecutionStep,
    attempt: CapabilityAttempt,
    definition: CapabilityDefinition | None,
    *,
    wait_expired: bool = False,
) -> PlanResultClassification:
    """Classify one planned result exactly once, by the fixed precedence.

    Preconditions are not this result's business: they are checked once, at
    the boundary before the next dispatch, by `plan_invalidation_facts`.
    """
    facts: list[str] = []
    result = attempt.result
    observation = definition is not None and definition.plan_observation
    interrupted = (attempt.disposition is CapabilityAttemptDisposition.PENDING
                   or (result is not None
                       and (result.failure or {}).get("code") == "dispatch_interrupted"))
    if interrupted:
        # An observation can simply be made again; anything else may already
        # have acted, and only she can say what to do about that.
        if observation and step.wait_seconds and not wait_expired:
            return PlanResultClassification(PlanResultKind.WAIT)
        facts.append("dispatch_interrupted")
    elif attempt.disposition is CapabilityAttemptDisposition.REJECTED:
        facts.append("planned_call_refused")
    elif result is None:
        facts.append("planned_result_missing")
    if facts:
        return PlanResultClassification(PlanResultKind.WAKE_CORE, tuple(facts))
    assert result is not None
    outcome = outcome_of(result)
    if outcome is ExecutionOutcome.FAILURE:
        return PlanResultClassification(PlanResultKind.WAKE_CORE, ("planned_result_failed",))
    if outcome is ExecutionOutcome.AMBIGUOUS:
        return PlanResultClassification(
            PlanResultKind.WAKE_CORE, ("planned_evidence_requires_judgement",))
    if outcome in (ExecutionOutcome.PENDING, ExecutionOutcome.TEMPORARILY_UNAVAILABLE):
        if not observation:
            # Only a declared observation may say "not yet"; from anything
            # else it is a result nobody declared, and she reads it.
            return PlanResultClassification(
                PlanResultKind.WAKE_CORE, ("planned_result_unexpected",))
        if not step.wait_seconds:
            return PlanResultClassification(PlanResultKind.WAKE_CORE, ("plan_step_pending",))
        if wait_expired:
            return PlanResultClassification(PlanResultKind.WAKE_CORE, ("plan_wait_exceeded",))
        return PlanResultClassification(PlanResultKind.WAIT)
    if conditions_match(_document(attempt), step.completion_conditions):
        return PlanResultClassification(PlanResultKind.ADVANCE)
    return PlanResultClassification(PlanResultKind.WAKE_CORE, ("planned_result_unexpected",))


def reduce_plan(
    plan: ExecutionPlan, classification: PlanResultClassification, now: datetime,
    *, evidence_call_id: str | None = None,
) -> ExecutionPlan:
    """Apply one classification: cursor, status, wait, or a new attention."""
    if classification.kind is PlanResultKind.WAKE_CORE:
        return raise_attention(
            plan, classification.facts, now,
            () if evidence_call_id is None else (evidence_call_id,),
        )
    step = plan.steps[plan.cursor]
    if classification.kind is PlanResultKind.WAIT:
        return replace(
            plan, status=PlanStatus.WAITING, inflight=None,
            next_due_at=now + timedelta(seconds=step.wait_seconds),
            wait_deadline=(plan.wait_deadline
                           or now + timedelta(seconds=step.max_wait_seconds)),
        )
    evidence = () if evidence_call_id is None else (evidence_call_id,)
    cursor = plan.cursor + 1
    if cursor == len(plan.steps):
        return raise_attention(plan, ("plan_steps_done",), now, evidence, cursor=cursor)
    if step.wake_core_on_completion:
        return raise_attention(plan, ("plan_checkpoint",), now, evidence, cursor=cursor)
    return replace(plan, cursor=cursor, status=PlanStatus.RUNNING, inflight=None,
                   next_due_at=None, wait_deadline=None)


def raise_attention(
    plan: ExecutionPlan, facts: tuple[str, ...], now: datetime,
    evidence_call_ids: tuple[str, ...] = (), *, cursor: int | None = None,
) -> ExecutionPlan:
    """RUNNING or WAITING to NEEDS_CORE: the only way a plan wakes her.

    `cursor` moves past a step that completed in the same transition.
    """
    seq = plan.attention_seq + 1
    return replace(
        plan, cursor=plan.cursor if cursor is None else cursor,
        status=PlanStatus.NEEDS_CORE, inflight=None, next_due_at=None,
        wait_deadline=None, attention_seq=seq,
        attention=PlanAttention(seq, facts[0], facts, now, evidence_call_ids,
                                next_offer_at=now),
    )


def resume_plan(plan: ExecutionPlan, *, accept_current: bool = False) -> ExecutionPlan:
    """Her answer that the same plan runs on: from its cursor, or after it.

    Accepting takes the step at the cursor as done on her judgment, which is
    how a result she was woken to judge lets the plan continue past it.
    """
    return replace(plan, cursor=plan.cursor + int(accept_current),
                   status=PlanStatus.RUNNING, attention=None)


def finish_plan(plan: ExecutionPlan, status: PlanStatus) -> ExecutionPlan:
    """COMPLETED or CANCELLED: hers to say, or a finished goal's to imply."""
    if status not in (PlanStatus.COMPLETED, PlanStatus.CANCELLED):
        raise ValueError("a plan finishes completed or cancelled")
    return replace(plan, status=status, inflight=None, next_due_at=None,
                   wait_deadline=None, attention=None)


def defer_plan(plan: ExecutionPlan, until: datetime) -> ExecutionPlan:
    """Hold a step that could not be dispatched yet, without waking her.

    Used when the execution budget refuses the dispatch: waking a paid
    reasoner because spending has stopped would defeat the point.
    """
    return replace(plan, status=PlanStatus.WAITING, inflight=None, next_due_at=until)


def conditions_match(document: Mapping[str, Any],
                     conditions: tuple[PlanCondition, ...]) -> bool:
    return all(condition_matches(document, item) for item in conditions)


def condition_matches(document: Any, condition: PlanCondition) -> bool:
    value = document
    for part in condition.path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            # A field the result does not have never satisfies a condition,
            # negated or not: absence is not a value.
            return False
        value = value[part]
    matched = json_equal(value, condition.equals)
    return not matched if condition.negate else matched


def json_equal(left: Any, right: Any) -> bool:
    """Equality as JSON defines it, which Python's == does not.

    A boolean equals only a boolean, so true is never 1 and false never 0,
    however deeply nested. Other numbers compare as JSON numbers, so 1 equals
    1.0. Null equals only null. Objects need identical keys and equal values;
    arrays need equal length and equal values in order, whether the result
    was frozen to a tuple or arrived as a list.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, Mapping) or isinstance(right, Mapping):
        return (isinstance(left, Mapping) and isinstance(right, Mapping)
                and set(left) == set(right)
                and all(json_equal(left[key], right[key]) for key in left))
    if isinstance(left, (tuple, list)) or isinstance(right, (tuple, list)):
        return (isinstance(left, (tuple, list)) and isinstance(right, (tuple, list))
                and len(left) == len(right)
                and all(json_equal(a, b) for a, b in zip(left, right)))
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    return type(left) is type(right) and left == right


def _document(attempt: CapabilityAttempt) -> Mapping[str, Any]:
    result = attempt.result
    return {
        "state": None if result is None else result.state.value,
        "values": {} if result is None else result.values,
        "failure": {} if result is None or result.failure is None else result.failure,
    }


__all__ = [
    "PlanResultClassification",
    "PlanResultKind",
    "classify_planned_result",
    "condition_matches",
    "conditions_match",
    "defer_plan",
    "finish_plan",
    "json_equal",
    "plan_invalidation_facts",
    "raise_attention",
    "reduce_plan",
    "resume_plan",
]
