"""The one interpretation of a planned capability result, and its one reduction.

D-036 lets deterministic code advance a plan AL/X already decided. Every
result that plan produces, whether fresh, recovered after a crash, or the
next observation of a wait, is classified here exactly once, before anything
waits or moves the cursor. Nothing else in the runtime may read a raw planned
result to decide whether to advance, wait, or wake her.

The precedence is fixed, and the first rule that applies sets the primary
reason; later rules still contribute their facts, so one wake carries all of
them:

1. the plan was changed, cancelled, or invalidated;
2. the dispatch was interrupted, uncertain, or never checkpointed;
3. the call was refused;
4. the call failed terminally, including a settled check that contradicts
   completion while others are still pending;
5. the evidence needs AL/X's judgment;
6. a pending state the plan declared, and the capability supports, is waiting;
7. a result known to satisfy every declared completion condition advances;
8. anything else is ambiguous and wakes her.

Rules 1 to 5 always outrank waiting and completion. Only rule 6 may wait, and
only rule 7 may advance.
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
    CapabilityResultState,
    ExecutionPlan,
    ExecutionStep,
    GoalState,
    GoalStatus,
    PlanCondition,
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
    # A review or merge result whose failure the reporting capability says
    # needs judgment. It blocks follow-up actions until AL/X has responded.
    blocker: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "facts", tuple(dict.fromkeys(self.facts)))
        if self.kind is PlanResultKind.WAKE_CORE and not self.facts:
            raise ValueError("a Core wake must name what woke her")
        if self.kind is not PlanResultKind.WAKE_CORE and (self.facts or self.blocker):
            raise ValueError("only a Core wake carries facts")


# Capabilities whose judgment-requiring failure blocks follow-up action. The
# same rule applies to a direct call and a planned one.
_BLOCKING_CAPABILITIES = frozenset({
    "request_external_review", "read_external_review", "merge_pull_request",
})
# The only keys a declared pending failure may carry. Anything more is detail
# the plan did not declare, and detail is for AL/X to read.
_PENDING_FAILURE_KEYS = frozenset({"code", "reason", "requires_judgement"})


def judgment_blocker(attempt: CapabilityAttempt) -> str | None:
    """The blocker a review or merge result imposes, if it imposes one."""
    if (attempt.call is None or attempt.call.capability_id not in _BLOCKING_CAPABILITIES
            or attempt.result is None):
        return None
    failure = attempt.result.failure or {}
    if not failure.get("requires_judgement"):
        return None
    code = failure.get("code")
    return str(code) if code is not None else "planned_result_unexpected"


def plan_invalidation_facts(
    plan: ExecutionPlan, state: GoalState, latest_person_turn_id: str | None,
    now: datetime, *, next_step: ExecutionStep | None = None,
) -> tuple[str, ...]:
    """Rule 1: why the plan AL/X decided no longer describes this goal."""
    facts = []
    if state.status is not GoalStatus.ACTIVE:
        facts.append("goal_inactive")
    if (state.objective.source_reference != plan.objective_source
            or state.objective.summary != plan.objective_summary
            or any(state.context.get(key) != value
                   for key, value in plan.context_preconditions.items())):
        facts.append("plan_precondition_changed")
    if latest_person_turn_id != plan.source_turn_id:
        facts.append("new_person_turn")
    if next_step is not None and next_step.call.approval_id is not None:
        approval = next(
            (item for item in state.approvals
             if item.approval_id == next_step.call.approval_id), None,
        )
        if (approval is None or approval.lifecycle is not ApprovalLifecycle.GRANTED
                or not approval.scope.matches(next_step.call)
                or (approval.expires_at is not None and approval.expires_at <= now)):
            facts.append("plan_approval_invalid")
    return tuple(facts)


def declared_pending_failure(
    definition: CapabilityDefinition | None, attempt: CapabilityAttempt,
) -> bool:
    """Whether a failure is exactly one the capability declares temporary."""
    result = attempt.result
    if (definition is None or result is None
            or result.state is not CapabilityResultState.FAILED
            or result.values or not result.failure):
        return False
    failure = result.failure
    return (set(failure) <= _PENDING_FAILURE_KEYS
            and (failure.get("code"), failure.get("reason"))
            in definition.pending_failure_reasons)


def classify_planned_result(
    step: ExecutionStep,
    attempt: CapabilityAttempt,
    definition: CapabilityDefinition | None,
    *,
    invalidation: tuple[str, ...] = (),
    recovered: bool = False,
    judgment_wake: tuple[str, ...] = (),
) -> PlanResultClassification:
    """Classify one planned result exactly once, by the fixed precedence.

    `judgment_wake` names the facts of a Core wake already under way, when
    this result re-reads the evidence that wake needs after a restart lost
    it. Such a result can only add to that wake: it never waits or advances.
    When it carries no evidence for her to judge, the evidence stays
    unavailable and holds follow-up action.
    """
    if judgment_wake:
        reread = classify_planned_result(
            step, attempt, definition, invalidation=invalidation, recovered=recovered,
        )
        facts = (*judgment_wake, "judgment_evidence_reobserved", *reread.facts)
        if "planned_evidence_requires_judgement" in reread.facts:
            return PlanResultClassification(PlanResultKind.WAKE_CORE, facts, reread.blocker)
        return unavailable_judgment_evidence(facts, blocker=reread.blocker)
    facts: list[str] = list(invalidation)
    result = attempt.result
    pending_failure = declared_pending_failure(definition, attempt)
    blocker = None if pending_failure else judgment_blocker(attempt)
    # Rule 2: what happened is not certain, or was never checkpointed.
    failure_code = None if result is None else (result.failure or {}).get("code")
    uncertain = (attempt.disposition is CapabilityAttemptDisposition.PENDING
                 or failure_code == "dispatch_interrupted"
                 or (attempt.disposition is CapabilityAttemptDisposition.BROKER_FAILURE
                     and attempt.implementation_invoked))
    if uncertain:
        facts.append("dispatch_interrupted" if failure_code == "dispatch_interrupted"
                     or attempt.disposition is CapabilityAttemptDisposition.PENDING
                     else "dispatch_uncertain")
    if recovered:
        facts.append("plan_result_uncheckpointed")
    if attempt.call is None or attempt.call.capability_id != step.call.capability_id:
        facts.append("planned_result_mismatch")
    # Rule 3: a refusal is never progress and never a reason to wait.
    if attempt.disposition is CapabilityAttemptDisposition.REJECTED:
        facts.append("planned_call_refused")
    # Rule 4: terminal failure.
    if attempt.disposition is CapabilityAttemptDisposition.BROKER_FAILURE and not uncertain:
        facts.append("planned_dispatch_failed")
    if (attempt.disposition is CapabilityAttemptDisposition.EXECUTED
            and result is not None and result.state is CapabilityResultState.FAILED
            and not pending_failure):
        facts.append("planned_result_failed")
    document = _document(attempt)
    waiting = bool(step.waiting_conditions) and conditions_match(
        document, step.waiting_conditions)
    if waiting and settled_contradiction(document, step):
        facts.append("planned_check_failed")
    # Rule 5: evidence AL/X must judge outranks both waiting and completion.
    if result is not None and not pending_failure and (
        (definition is not None and definition.requires_core_judgment
         and result.state is CapabilityResultState.SUCCEEDED)
        or (result.failure or {}).get("requires_judgement")
    ):
        facts.append("planned_evidence_requires_judgement")
    if facts:
        return PlanResultClassification(PlanResultKind.WAKE_CORE, tuple(facts), blocker)
    if attempt.disposition is not CapabilityAttemptDisposition.EXECUTED or result is None:
        return PlanResultClassification(PlanResultKind.WAKE_CORE,
                                        ("planned_result_missing",))
    completed = (result.state is CapabilityResultState.SUCCEEDED and not result.failure
                 and conditions_match(document, step.completion_conditions))
    if waiting and completed:
        return PlanResultClassification(PlanResultKind.WAKE_CORE,
                                        ("planned_result_conflicting",))
    # Rule 6: only a declared pending state of an observation may wait.
    if waiting and (pending_failure or (
            result.state is CapabilityResultState.SUCCEEDED and not result.failure)):
        return PlanResultClassification(PlanResultKind.WAIT)
    # Rule 7: a known-safe completion.
    if completed:
        return PlanResultClassification(PlanResultKind.ADVANCE)
    # Rule 8: partial, missing, or anything the plan did not declare.
    if result.state is CapabilityResultState.PARTIAL:
        return PlanResultClassification(PlanResultKind.WAKE_CORE,
                                        ("planned_result_partial",))
    return PlanResultClassification(PlanResultKind.WAKE_CORE,
                                    ("planned_result_unexpected",))


def reduce_plan(
    plan: ExecutionPlan, classification: PlanResultClassification, now: datetime,
    *, result_call_id: str | None = None,
) -> ExecutionPlan:
    """Apply one classification: cursor, status, result identity, wait, wake."""
    last = plan.last_result_call_id if result_call_id is None else result_call_id
    if classification.kind is PlanResultKind.WAKE_CORE:
        return replace(
            plan, status="needs_core", next_due_at=None,
            core_reentry_reason=classification.facts[0],
            core_reentry_facts=classification.facts,
            mechanical_blocker=classification.blocker,
            last_result_call_id=last,
        )
    step = plan.steps[plan.cursor]
    if classification.kind is PlanResultKind.WAIT:
        return replace(
            plan, status="waiting",
            next_due_at=now + timedelta(seconds=step.wait_seconds),
            core_reentry_reason=None, core_reentry_facts=(),
            mechanical_blocker=None, last_result_call_id=last,
        )
    advanced = replace(
        plan, cursor=plan.cursor + 1, status="ready", next_due_at=None,
        core_reentry_reason=None, core_reentry_facts=(),
        mechanical_blocker=None, last_result_call_id=last,
    )
    if step.wake_core_on_completion:
        return reduce_plan(advanced, PlanResultClassification(
            PlanResultKind.WAKE_CORE, ("planned_evidence_requires_judgement",),
        ), now)
    if advanced.cursor == len(advanced.steps):
        return replace(advanced, status="completed",
                       core_reentry_reason="plan_completed",
                       core_reentry_facts=("plan_completed",))
    return advanced


def wake(*facts: str, blocker: str | None = None) -> PlanResultClassification:
    """A Core wake that no capability result produced."""
    return PlanResultClassification(PlanResultKind.WAKE_CORE, facts, blocker)


def unavailable_judgment_evidence(
    facts: tuple[str, ...], *more: str, blocker: str | None = None,
) -> PlanResultClassification:
    """A judgment wake whose evidence cannot be had: held until she responds."""
    return PlanResultClassification(
        PlanResultKind.WAKE_CORE,
        (*facts, "judgment_evidence_unavailable", *more),
        blocker or "judgment_evidence_unavailable",
    )


def conditions_match(document: Mapping[str, Any],
                     conditions: tuple[PlanCondition, ...]) -> bool:
    return all(condition_matches(document, item) for item in conditions)


def condition_matches(document: Any, condition: PlanCondition) -> bool:
    values: list[Any] = [document]
    for part in condition.path.split("."):
        next_values = []
        for value in values:
            if part == "*" and isinstance(value, (tuple, list)) and value:
                next_values.extend(value)
            elif isinstance(value, Mapping) and part in value:
                next_values.append(value[part])
            else:
                return False
        values = next_values
        if not values:
            return False
    comparisons = (not json_equal(value, condition.equals) if condition.negate
                   else json_equal(value, condition.equals) for value in values)
    return any(comparisons) if condition.quantifier == "any" else all(comparisons)


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


def settled_contradiction(document: Mapping[str, Any], step: ExecutionStep) -> bool:
    """Whether a settled element already fails completion while others wait.

    A plan waits on a collection, such as check runs or commit statuses,
    because some members are still pending. A member that is not pending is
    settled, and a settled member that fails a completion condition is a
    failure now: waiting cannot repair it. Members of a collection the
    waiting conditions do not mention are all settled.
    """
    if any("*" not in item.path.split(".") for item in step.waiting_conditions):
        # The whole result is declared pending; nothing in it is settled yet.
        return False
    pending_by_prefix: dict[str, list[tuple[str, PlanCondition]]] = {}
    for item in step.waiting_conditions:
        prefix, suffix = _split_wildcard(item.path)
        pending_by_prefix.setdefault(prefix, []).append((suffix, item))
    for item in step.completion_conditions:
        if "*" not in item.path.split(".") or item.quantifier != "all":
            continue
        prefix, suffix = _split_wildcard(item.path)
        members = _resolve(document, prefix)
        if not isinstance(members, (tuple, list)):
            continue
        for member in members:
            if any(_member_matches(member, waiting_suffix, waiting)
                   for waiting_suffix, waiting in pending_by_prefix.get(prefix, ())):
                continue
            if not _member_matches(member, suffix, item):
                return True
    return False


def _member_matches(member: Any, suffix: str, condition: PlanCondition) -> bool:
    """Evaluate a wildcard condition against one member of its collection."""
    if not suffix:
        matched = json_equal(member, condition.equals)
        return not matched if condition.negate else matched
    return condition_matches(member, replace(condition, path=suffix))


def _split_wildcard(path: str) -> tuple[str, str]:
    parts = path.split(".")
    index = parts.index("*")
    return ".".join(parts[:index]), ".".join(parts[index + 1:])


def _resolve(document: Any, path: str) -> Any:
    value = document
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


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
    "declared_pending_failure",
    "json_equal",
    "judgment_blocker",
    "plan_invalidation_facts",
    "reduce_plan",
    "settled_contradiction",
    "unavailable_judgment_evidence",
    "wake",
]
