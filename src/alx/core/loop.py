"""The only iterative AL/X reasoning authority."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum

from alx.contracts import (
    ApprovalLifecycle,
    CapabilityAttempt,
    CapabilityAttemptDisposition,
    CapabilityDefinition,
    CapabilityDispatch,
    DurableGoalStore,
    DurableMemoryStore,
    GoalSnapshot,
    GoalState,
    GoalStatus,
    Objective,
    MemoryKind,
    MemoryProposal,
    ReasoningContext,
    ReasoningProvider,
    SuccessCriterion,
    ConversationTurn,
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
        memory_store: DurableMemoryStore | None = None,
    ) -> None:
        self._store = store
        self._reasoner = reasoner
        self._dispatch = dispatch
        self._capabilities = tuple(capabilities)
        self._memory_store = memory_store

    def start(
        self,
        goal_id: str,
        turn: ConversationTurn,
        retention_until: datetime,
        step_budget: int,
    ) -> CoreOutcome:
        """Persist a new utterance as a goal before asking the reasoner what it means."""
        self._validate_step_budget(step_budget)
        initial = GoalState(
            goal_id=goal_id,
            objective=Objective(turn.turn_id, turn.content),
            success_criteria=(SuccessCriterion(f"{goal_id}-criterion-1", turn.content),),
        )
        self._store.create(initial, (turn,), retention_until)
        return self.run(goal_id, step_budget)

    def continue_goal(
        self,
        goal_id: str,
        turn: ConversationTurn,
        step_budget: int,
    ) -> CoreOutcome:
        """Durably add a follow-up before the same Core reasons about it."""
        self._validate_step_budget(step_budget)
        snapshot = self._store.load(goal_id)
        if snapshot.state.status in (GoalStatus.COMPLETED, GoalStatus.CANCELLED):
            return CoreOutcome(CoreState.ERROR, snapshot, reason="goal_inactive")
        resumed = replace(snapshot.state, status=GoalStatus.ACTIVE, stop_reason=None)
        self._store.replace(
            resumed,
            (*snapshot.turns, turn),
            snapshot.retention_until,
            snapshot.revision,
        )
        return self.run(goal_id, step_budget)

    def run(self, goal_id: str, step_budget: int) -> CoreOutcome:
        self._validate_step_budget(step_budget)

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

            if not self._memory_proposals_are_grounded(snapshot, decision.memory_proposals):
                return CoreOutcome(CoreState.ERROR, snapshot, reason="memory_proposal_invalid")

            if decision.call is None:
                if decision.goal.status is GoalStatus.ACTIVE:
                    return CoreOutcome(
                        CoreState.ERROR,
                        snapshot,
                        reason="active_response",
                    )
                if not self._persist_memories_safely(decision.memory_proposals, snapshot.retention_until):
                    return CoreOutcome(CoreState.ERROR, snapshot, reason="memory_persistence_error")
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
            if not self._persist_memories_safely(decision.memory_proposals, snapshot.retention_until):
                return CoreOutcome(CoreState.ERROR, snapshot, reason="memory_persistence_error")

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

            snapshot = self._finalize_dispatch(snapshot, attempt)

        return CoreOutcome(
            CoreState.CHECKPOINTED,
            snapshot,
            reason="budget_exhausted",
        )

    def reconcile_dispatch(
        self,
        goal_id: str,
        attempt: CapabilityAttempt,
    ) -> CoreOutcome:
        """Record trusted evidence for a dispatch left uncertain by interruption."""
        snapshot = self._store.load(goal_id)
        pending = tuple(
            item
            for item in snapshot.state.attempts
            if item.disposition is CapabilityAttemptDisposition.PENDING
        )
        if len(pending) != 1:
            return CoreOutcome(
                CoreState.ERROR,
                snapshot,
                reason="pending_dispatch_missing",
            )
        if (
            attempt.disposition is CapabilityAttemptDisposition.PENDING
            or attempt.call != pending[0].call
        ):
            return CoreOutcome(CoreState.ERROR, snapshot, reason="attempt_invalid")
        snapshot = self._finalize_dispatch(snapshot, attempt)
        return CoreOutcome(
            CoreState.CHECKPOINTED,
            snapshot,
            reason="dispatch_reconciled",
        )

    def _finalize_dispatch(
        self,
        snapshot: GoalSnapshot,
        attempt: CapabilityAttempt,
    ) -> GoalSnapshot:
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
        return self._store.replace(
            updated,
            snapshot.turns,
            snapshot.retention_until,
            snapshot.revision,
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

    @staticmethod
    def _memory_proposals_are_grounded(
        snapshot: GoalSnapshot,
        proposals: tuple[MemoryProposal, ...],
    ) -> bool:
        turns = {f"turn:{item.turn_id}": item.person_id for item in snapshot.turns}
        state = snapshot.state
        references = {
            *turns,
            *(f"evidence:{item.evidence_id}" for item in state.evidence),
            *(f"decision:{item.record_id}" for item in state.decisions),
            *(f"correction:{item.record_id}" for item in state.corrections),
            *(f"progress:{item.record_id}" for item in state.progress),
            *(
                f"attempt:{item.call.call_id}"
                for item in state.attempts
                if item.call is not None
            ),
        }
        for proposal in proposals:
            if any(reference not in references for reference in proposal.source_references):
                return False
            if proposal.kind is MemoryKind.RELATIONSHIP:
                sourced_people = [
                    turns[reference]
                    for reference in proposal.source_references
                    if reference in turns
                ]
                if not sourced_people or any(
                    person_id != proposal.person_id for person_id in sourced_people
                ):
                    return False
        return True

    def _persist_memories_safely(
        self,
        proposals: tuple[MemoryProposal, ...],
        retention_until: datetime,
    ) -> bool:
        try:
            self._persist_memories(proposals, retention_until)
        except Exception:
            return False
        return True

    def _persist_memories(
        self,
        proposals: tuple[MemoryProposal, ...],
        retention_until: datetime,
    ) -> None:
        if proposals and self._memory_store is None:
            raise RuntimeError("memory store is required for Core memory proposals")
        if self._memory_store is not None:
            for proposal in proposals:
                self._memory_store.remember(proposal, retention_until)

    @staticmethod
    def _validate_step_budget(step_budget: int) -> None:
        if step_budget <= 0:
            raise ValueError("step_budget must be positive")
