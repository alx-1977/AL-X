"""The only iterative AL/X reasoning authority."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from alx.contracts import (
    ApprovalLifecycle,
    CapabilityAttempt,
    CapabilityAttemptDisposition,
    CapabilityDefinition,
    CapabilityDispatch,
    DurableGoalStore,
    GoalSnapshot,
    GoalState,
    GoalStatus,
    ReasoningContext,
    ReasoningProvider,
)


class CoreState(str, Enum):
    RESPONDED = "responded"
    CHECKPOINTED = "checkpointed"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class CoreOutcome:
    state: CoreState
    snapshot: GoalSnapshot
    response: str | None = None
    reason: str | None = None


class CoreAgent:
    def __init__(
        self,
        store: DurableGoalStore,
        reasoner: ReasoningProvider,
        dispatch: CapabilityDispatch,
        capabilities: tuple[CapabilityDefinition, ...],
    ) -> None:
        self._store = store
        self._reasoner = reasoner
        self._dispatch = dispatch
        self._capabilities = tuple(capabilities)

    def run(self, goal_id: str, step_budget: int) -> CoreOutcome:
        if step_budget <= 0:
            raise ValueError("step_budget must be positive")

        snapshot = self._store.load(goal_id)
        if snapshot.state.status in (GoalStatus.COMPLETED, GoalStatus.CANCELLED):
            return CoreOutcome(CoreState.ERROR, snapshot, reason="goal_inactive")
        if self._has_pending_dispatch(snapshot.state):
            return CoreOutcome(CoreState.ERROR, snapshot, reason="dispatch_unresolved")

        for _ in range(step_budget):
            try:
                decision = self._reasoner.decide(
                    ReasoningContext(
                        snapshot.state,
                        snapshot.turns,
                        self._capabilities,
                    )
                )
            except Exception:
                return CoreOutcome(CoreState.ERROR, snapshot, reason="reasoner_error")

            if (
                decision.goal.goal_id != goal_id
                or decision.goal.attempts != snapshot.state.attempts
                or decision.goal.approvals != snapshot.state.approvals
                or not self._preserves_history(snapshot.state, decision.goal)
            ):
                return CoreOutcome(CoreState.ERROR, snapshot, reason="decision_invalid")

            if decision.call is None:
                if decision.goal.status is GoalStatus.ACTIVE:
                    return CoreOutcome(
                        CoreState.ERROR,
                        snapshot,
                        reason="active_response",
                    )
                snapshot = self._store.replace(
                    decision.goal,
                    snapshot.turns,
                    snapshot.retention_until,
                    snapshot.revision,
                )
                return CoreOutcome(
                    CoreState.RESPONDED,
                    snapshot,
                    response=decision.response,
                )

            if decision.goal.status is not GoalStatus.ACTIVE:
                return CoreOutcome(CoreState.ERROR, snapshot, reason="inactive_call")
            if self._call_id_exists(snapshot.state, decision.call.call_id):
                return CoreOutcome(CoreState.ERROR, snapshot, reason="call_id_reused")

            pending = CapabilityAttempt(
                decision.call,
                CapabilityAttemptDisposition.PENDING,
                None,
                reason_code="dispatch_pending",
            )
            claimed_approvals = tuple(
                replace(item, lifecycle=ApprovalLifecycle.CLAIMED)
                if (
                    item.lifecycle is ApprovalLifecycle.GRANTED
                    and item.approval_id == decision.call.approval_id
                    and item.scope.matches(decision.call)
                )
                else item
                for item in decision.goal.approvals
            )
            checkpoint = replace(
                decision.goal,
                attempts=(*decision.goal.attempts, pending),
                approvals=claimed_approvals,
            )
            snapshot = self._store.replace(
                checkpoint,
                snapshot.turns,
                snapshot.retention_until,
                snapshot.revision,
            )
            try:
                attempt = self._dispatch(decision.call, decision.goal)
            except Exception:
                return CoreOutcome(CoreState.ERROR, snapshot, reason="dispatch_error")
            if attempt.call != decision.call:
                return CoreOutcome(CoreState.ERROR, snapshot, reason="attempt_invalid")

            approvals = tuple(
                replace(
                    item,
                    lifecycle=(
                        ApprovalLifecycle.CONSUMED
                        if attempt.implementation_invoked
                        else ApprovalLifecycle.GRANTED
                    ),
                )
                if (
                    item.lifecycle is ApprovalLifecycle.CLAIMED
                    and item.approval_id == attempt.call.approval_id
                    and item.scope.matches(attempt.call)
                )
                else item
                for item in snapshot.state.approvals
            )
            updated = replace(
                snapshot.state,
                attempts=(*snapshot.state.attempts[:-1], attempt),
                approvals=approvals,
            )
            snapshot = self._store.replace(
                updated,
                snapshot.turns,
                snapshot.retention_until,
                snapshot.revision,
            )

        return CoreOutcome(
            CoreState.CHECKPOINTED,
            snapshot,
            reason="budget_exhausted",
        )

    @staticmethod
    def _preserves_history(previous: GoalState, updated: GoalState) -> bool:
        return all(
            getattr(updated, name)[: len(getattr(previous, name))]
            == getattr(previous, name)
            for name in ("corrections", "decisions", "progress", "evidence")
        )

    @staticmethod
    def _call_id_exists(state: GoalState, call_id: str) -> bool:
        return any(
            (attempt.call is not None and attempt.call.call_id == call_id)
            or (
                attempt.call is None
                and attempt.result is not None
                and attempt.result.call_id == call_id
            )
            for attempt in state.attempts
        )

    @staticmethod
    def _has_pending_dispatch(state: GoalState) -> bool:
        return any(
            attempt.disposition is CapabilityAttemptDisposition.PENDING
            for attempt in state.attempts
        )
