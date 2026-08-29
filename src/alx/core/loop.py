"""The only iterative AL/X reasoning authority."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
import logging
from uuid import uuid4

from alx.contracts import (
    ApprovalLifecycle, CapabilityAttempt, CapabilityAttemptDisposition,
    CapabilityDefinition, CapabilityDispatch, ConversationSnapshot,
    DurableGoalStore, DurableMemoryStore, GoalMutationKind, GoalProposal,
    GoalSnapshot, GoalState, GoalStatus, GoalStopReason, MemoryKind,
    MemoryProposal, MemoryQuery, MemorySnapshot, Objective, ReasoningContext,
    ReasoningProvider,
)

LOGGER = logging.getLogger(__name__)


class CoreState(str, Enum):
    RESPONDED = "responded"
    CHECKPOINTED = "checkpointed"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class CoreOutcome:
    state: CoreState
    snapshot: GoalSnapshot | None = None
    response: str | None = None
    reason: str | None = None


class CoreAgent:
    def __init__(self, store: DurableGoalStore, reasoner: ReasoningProvider,
                 dispatch: CapabilityDispatch,
                 capabilities: tuple[CapabilityDefinition, ...],
                 memory_store: DurableMemoryStore | None = None,
                 clock: Callable[[], datetime] | None = None,
                 identifier_factory: Callable[[], str] | None = None) -> None:
        self._store = store
        self._reasoner = reasoner
        self._dispatch = dispatch
        self._capabilities = tuple(capabilities)
        self._memory_store = memory_store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._identifier_factory = identifier_factory or (lambda: str(uuid4()))

    def process(self, conversation: ConversationSnapshot, goal_id: str | None,
                retention_until: datetime, step_budget: int) -> CoreOutcome:
        """Reason over one durable conversation and its optional attached goal."""
        self._validate_step_budget(step_budget)
        snapshot = None if goal_id is None else self._store.load(goal_id)
        if snapshot is not None and snapshot.conversation_id != conversation.conversation_id:
            return CoreOutcome(CoreState.ERROR, snapshot, reason="goal_conversation_mismatch")
        if snapshot is not None and self._has_pending_dispatch(snapshot.state):
            return CoreOutcome(CoreState.ERROR, snapshot, reason="dispatch_unresolved")
        if snapshot is not None and not self._flush_pending_memory_batches(snapshot.state.goal_id):
            return CoreOutcome(CoreState.ERROR, snapshot, reason="memory_persistence_error")

        retrieved_memories: tuple[MemorySnapshot, ...] = ()
        memory_query_ids: set[str] = set()
        for _ in range(step_budget):
            try:
                now = self._clock()
                if now.tzinfo is None or now.utcoffset() is None:
                    raise ValueError("Core clock must be timezone-aware")
            except Exception:
                return CoreOutcome(CoreState.ERROR, snapshot, reason="clock_error")
            try:
                decision = self._reasoner.decide(ReasoningContext(
                    None if snapshot is None else snapshot.state,
                    conversation.turns, self._capabilities, retrieved_memories,
                ))
            except Exception as error:
                LOGGER.info("Reasoner decision rejected: %s: %s", type(error).__name__, error)
                return CoreOutcome(CoreState.ERROR, snapshot, reason="reasoner_error")

            previous = snapshot
            candidate, proposal_error = self._reduce_goal_proposal(
                snapshot, decision.goal_proposal, conversation,
            )
            if proposal_error is not None:
                LOGGER.info("Goal proposal rejected: %s", proposal_error)
                if decision.response_requires_goal_commit:
                    return CoreOutcome(CoreState.ERROR, snapshot, reason="goal_proposal_invalid")

            memory_error = self._memory_proposal_grounding_error(
                conversation,
                (None if previous is None else previous.state)
                if proposal_error is not None else candidate,
                decision.memory_proposals, now,
            )
            if memory_error is not None:
                LOGGER.info("Memory proposal rejected: %s", memory_error)
                return CoreOutcome(CoreState.ERROR, snapshot, reason="memory_proposal_invalid")
            if proposal_error is None and decision.goal_proposal is not None:
                assert candidate is not None
                if previous is None:
                    snapshot = self._store.create(
                        candidate, conversation.conversation_id, retention_until)
                else:
                    snapshot = self._store.replace(
                        candidate, previous.retention_until, previous.revision)

            if decision.memory_query is not None:
                if decision.memory_query.query_id in memory_query_ids:
                    return CoreOutcome(CoreState.ERROR, snapshot, reason="memory_query_id_reused")
                if not self._memory_query_is_authorized(conversation, decision.memory_query):
                    return CoreOutcome(CoreState.ERROR, snapshot, reason="memory_query_unauthorized")
                snapshot, committed = self._commit_memories(
                    snapshot, decision.memory_proposals, retention_until)
                if not committed:
                    return CoreOutcome(CoreState.ERROR, snapshot, reason="memory_persistence_error")
                if self._memory_store is None:
                    return CoreOutcome(CoreState.ERROR, snapshot, reason="memory_store_unavailable")
                try:
                    retrieved_memories = self._memory_store.retrieve(decision.memory_query, now)
                except Exception:
                    return CoreOutcome(CoreState.ERROR, snapshot, reason="memory_retrieval_error")
                memory_query_ids.add(decision.memory_query.query_id)
                continue

            if decision.call is None:
                snapshot, committed = self._commit_memories(
                    snapshot, decision.memory_proposals, retention_until)
                if not committed:
                    return CoreOutcome(CoreState.ERROR, snapshot, reason="memory_persistence_error")
                return CoreOutcome(CoreState.RESPONDED, snapshot, response=decision.response,
                                   reason="goal_proposal_rejected" if proposal_error else None)

            if snapshot is None or snapshot.state.status is not GoalStatus.ACTIVE:
                return CoreOutcome(CoreState.ERROR, snapshot, reason="active_goal_required")
            if self._call_id_exists(snapshot.state, decision.call.call_id):
                return CoreOutcome(CoreState.ERROR, snapshot, reason="call_id_reused")
            pending = CapabilityAttempt(decision.call, CapabilityAttemptDisposition.PENDING,
                                        None, reason_code="dispatch_pending")
            approvals = tuple(
                replace(item, lifecycle=ApprovalLifecycle.CLAIMED)
                if (item.lifecycle is ApprovalLifecycle.GRANTED
                    and item.approval_id == decision.call.approval_id
                    and item.scope.matches(decision.call)) else item
                for item in snapshot.state.approvals
            )
            checkpoint = replace(snapshot.state,
                                 attempts=(*snapshot.state.attempts, pending),
                                 approvals=approvals)
            snapshot = self._store.replace(checkpoint, snapshot.retention_until,
                                           snapshot.revision)
            snapshot, committed = self._commit_memories(
                snapshot, decision.memory_proposals, retention_until)
            if not committed:
                return CoreOutcome(CoreState.ERROR, snapshot, reason="memory_persistence_error")
            try:
                attempt = self._dispatch(decision.call, checkpoint)
            except Exception:
                return CoreOutcome(CoreState.ERROR, snapshot, reason="dispatch_error")
            if attempt.call != decision.call:
                return CoreOutcome(CoreState.ERROR, snapshot, reason="attempt_invalid")
            snapshot = self._finalize_dispatch(snapshot, attempt)
        return CoreOutcome(CoreState.CHECKPOINTED, snapshot, reason="budget_exhausted")

    def reconcile_dispatch(self, goal_id: str, attempt: CapabilityAttempt) -> CoreOutcome:
        snapshot = self._store.load(goal_id)
        pending = tuple(item for item in snapshot.state.attempts
                        if item.disposition is CapabilityAttemptDisposition.PENDING)
        if len(pending) != 1:
            return CoreOutcome(CoreState.ERROR, snapshot, reason="pending_dispatch_missing")
        if attempt.disposition is CapabilityAttemptDisposition.PENDING or attempt.call != pending[0].call:
            return CoreOutcome(CoreState.ERROR, snapshot, reason="attempt_invalid")
        snapshot = self._finalize_dispatch(snapshot, attempt)
        return CoreOutcome(CoreState.CHECKPOINTED, snapshot, reason="dispatch_reconciled")

    def _reduce_goal_proposal(self, snapshot: GoalSnapshot | None,
                              proposal: GoalProposal | None,
                              conversation: ConversationSnapshot) -> tuple[GoalState | None, str | None]:
        if proposal is None:
            return None if snapshot is None else snapshot.state, None
        if snapshot is None:
            if proposal.kind is not GoalMutationKind.CREATE:
                return None, "goal_missing"
            if proposal.objective_summary is None or not proposal.success_criteria:
                return None, "goal_creation_incomplete"
            creation_error = self._new_history_error(proposal)
            if creation_error:
                return None, creation_error
            state = GoalState(
                self._identifier_factory(),
                Objective(f"turn:{conversation.turns[-1].turn_id}", proposal.objective_summary),
                proposal.success_criteria,
                context={} if proposal.context is None else proposal.context,
                referents=() if proposal.referents is None else proposal.referents,
                decisions=proposal.new_decisions, corrections=proposal.new_corrections,
                progress=proposal.new_progress,
                blockers=() if proposal.blockers is None else proposal.blockers,
                outstanding_work=() if proposal.outstanding_work is None else proposal.outstanding_work,
                evidence=proposal.new_evidence,
            )
            error = self._evidence_grounding_error(conversation, state, (), proposal.new_evidence)
            if error:
                return None, error
            return state, None

        if proposal.kind is GoalMutationKind.CREATE:
            return snapshot.state, "goal_already_active"
        state = snapshot.state
        if state.status in (GoalStatus.COMPLETED, GoalStatus.CANCELLED):
            return state, "goal_inactive"
        history_error = self._history_proposal_error(state, proposal)
        if history_error:
            return state, history_error
        try:
            updated = replace(
                state,
                objective=state.objective if proposal.objective_summary is None else Objective(state.objective.source_reference, proposal.objective_summary),
                success_criteria=state.success_criteria if proposal.success_criteria is None else proposal.success_criteria,
                context=state.context if proposal.context is None else proposal.context,
                referents=state.referents if proposal.referents is None else proposal.referents,
                decisions=(*state.decisions, *proposal.new_decisions),
                corrections=(*state.corrections, *proposal.new_corrections),
                progress=(*state.progress, *proposal.new_progress),
                blockers=state.blockers if proposal.blockers is None else proposal.blockers,
                outstanding_work=state.outstanding_work if proposal.outstanding_work is None else proposal.outstanding_work,
                evidence=(*state.evidence, *proposal.new_evidence),
                status=GoalStatus.ACTIVE, stop_reason=None,
            )
            error = self._evidence_grounding_error(
                conversation, updated, state.evidence, proposal.new_evidence)
            if error:
                return state, error
            updated = self._derive_goal_status(updated, proposal.kind)
        except (TypeError, ValueError) as error_value:
            return state, str(error_value)
        return updated, None

    @staticmethod
    def _history_proposal_error(state: GoalState, proposal: GoalProposal) -> str | None:
        proposal_error = CoreAgent._new_history_error(
            proposal, {item.evidence_id for item in state.evidence})
        if proposal_error:
            return proposal_error
        for existing, proposed, attribute in (
            (state.decisions, proposal.new_decisions, "record_id"),
            (state.corrections, proposal.new_corrections, "record_id"),
            (state.progress, proposal.new_progress, "record_id"),
            (state.evidence, proposal.new_evidence, "evidence_id"),
        ):
            existing_ids = {getattr(item, attribute) for item in existing}
            proposed_ids = [getattr(item, attribute) for item in proposed]
            if len(proposed_ids) != len(set(proposed_ids)) or existing_ids.intersection(proposed_ids):
                return "durable_record_id_reused"
        evidence_ids = {
            *(item.evidence_id for item in state.evidence),
            *(item.evidence_id for item in proposal.new_evidence),
        }
        for record in (
            *proposal.new_decisions,
            *proposal.new_corrections,
            *proposal.new_progress,
        ):
            if any(reference not in evidence_ids for reference in record.evidence_refs):
                return "history_evidence_unknown"
        return None

    @staticmethod
    def _new_history_error(
        proposal: GoalProposal,
        existing_evidence_ids: set[str] | None = None,
    ) -> str | None:
        for proposed, attribute in (
            (proposal.new_decisions, "record_id"),
            (proposal.new_corrections, "record_id"),
            (proposal.new_progress, "record_id"),
            (proposal.new_evidence, "evidence_id"),
        ):
            identifiers = [getattr(item, attribute) for item in proposed]
            if len(identifiers) != len(set(identifiers)):
                return "durable_record_id_reused"
        evidence_ids = set() if existing_evidence_ids is None else set(existing_evidence_ids)
        evidence_ids.update(item.evidence_id for item in proposal.new_evidence)
        for record in (
            *proposal.new_decisions,
            *proposal.new_corrections,
            *proposal.new_progress,
        ):
            if any(reference not in evidence_ids for reference in record.evidence_refs):
                return "history_evidence_unknown"
        return None

    @staticmethod
    def _derive_goal_status(state: GoalState, kind: GoalMutationKind) -> GoalState:
        if kind is GoalMutationKind.REQUEST_COMPLETION:
            if state.blockers or state.outstanding_work or any(
                item.disposition is CapabilityAttemptDisposition.PENDING for item in state.attempts):
                raise ValueError("completion_has_unresolved_work")
            supported = {criterion for item in state.evidence if item.source_references
                         for criterion in item.supports}
            required = {item.criterion_id for item in state.success_criteria}
            if not required.issubset(supported):
                raise ValueError("completion_lacks_sourced_evidence")
            return replace(state, status=GoalStatus.COMPLETED,
                           stop_reason=GoalStopReason.SUCCESS_CRITERIA_MET)
        if kind is GoalMutationKind.AWAIT_INPUT:
            return replace(state, status=GoalStatus.AWAITING_INPUT,
                           stop_reason=GoalStopReason.REQUIRED_INPUT)
        if kind is GoalMutationKind.AWAIT_APPROVAL:
            return replace(state, status=GoalStatus.AWAITING_APPROVAL,
                           stop_reason=GoalStopReason.REQUIRED_APPROVAL)
        if kind is GoalMutationKind.BLOCK:
            return replace(state, status=GoalStatus.BLOCKED,
                           stop_reason=GoalStopReason.GENUINELY_BLOCKED)
        if kind is GoalMutationKind.CANCEL:
            return replace(state, status=GoalStatus.CANCELLED,
                           stop_reason=GoalStopReason.CANCELLED)
        return state

    @staticmethod
    def _evidence_grounding_error(conversation: ConversationSnapshot,
                                  state: GoalState, existing: tuple,
                                  proposed: tuple) -> str | None:
        known = {f"turn:{item.turn_id}" for item in conversation.turns}
        known.update(f"attempt:{item.call.call_id}" for item in state.attempts
                     if item.call is not None
                     and item.disposition is not CapabilityAttemptDisposition.PENDING
                     and item.result is not None)
        known.update(f"evidence:{item.evidence_id}" for item in existing)
        criteria = {item.criterion_id for item in state.success_criteria}
        seen = {item.evidence_id for item in existing}
        for item in proposed:
            if item.evidence_id in seen:
                return "evidence_id_reused"
            seen.add(item.evidence_id)
            if not item.source_references:
                return "evidence_source_required"
            if any(reference not in known for reference in item.source_references):
                return "evidence_source_unknown"
            if any(reference not in criteria for reference in item.supports):
                return "evidence_support_unknown"
            known.add(f"evidence:{item.evidence_id}")
        return None

    def _finalize_dispatch(self, snapshot: GoalSnapshot,
                           attempt: CapabilityAttempt) -> GoalSnapshot:
        assert attempt.call is not None
        approvals = tuple(
            replace(item, lifecycle=(ApprovalLifecycle.CONSUMED
                                     if attempt.implementation_invoked
                                     else ApprovalLifecycle.GRANTED))
            if (item.lifecycle is ApprovalLifecycle.CLAIMED
                and item.approval_id == attempt.call.approval_id
                and item.scope.matches(attempt.call)) else item
            for item in snapshot.state.approvals
        )
        updated = replace(snapshot.state,
                          attempts=(*snapshot.state.attempts[:-1], attempt),
                          approvals=approvals)
        return self._store.replace(updated, snapshot.retention_until, snapshot.revision)

    def _commit_memories(self, snapshot: GoalSnapshot | None,
                         proposals: tuple[MemoryProposal, ...],
                         retention_until: datetime) -> tuple[GoalSnapshot | None, bool]:
        if not proposals:
            return snapshot, True
        if self._memory_store is None:
            return snapshot, False
        try:
            if snapshot is None:
                self._memory_store.remember_many(proposals, retention_until)
                return None, True
            updated = self._store.replace_with_memory_batch(
                snapshot.state, snapshot.retention_until, snapshot.revision, proposals)
            return updated, self._flush_pending_memory_batches(updated.state.goal_id)
        except Exception:
            return snapshot, False

    def _flush_pending_memory_batches(self, goal_id: str) -> bool:
        try:
            batches = self._store.pending_memory_batches(goal_id)
            if batches and self._memory_store is None:
                return False
            for batch in batches:
                assert self._memory_store is not None
                self._memory_store.remember_many(batch.proposals, batch.retention_until)
                self._store.acknowledge_memory_batch(batch.goal_id, batch.goal_revision)
        except Exception:
            return False
        return True

    @staticmethod
    def _memory_proposal_grounding_error(conversation: ConversationSnapshot,
                                         state: GoalState | None,
                                         proposals: tuple[MemoryProposal, ...],
                                         as_of: datetime) -> str | None:
        turns = {f"turn:{item.turn_id}": item.person_id for item in conversation.turns}
        turn_times = {f"turn:{item.turn_id}": item.occurred_at for item in conversation.turns}
        references = set(turns)
        if state is not None:
            references.update(f"evidence:{item.evidence_id}" for item in state.evidence)
            references.update(f"decision:{item.record_id}" for item in state.decisions)
            references.update(f"correction:{item.record_id}" for item in state.corrections)
            references.update(f"progress:{item.record_id}" for item in state.progress)
            references.update(f"attempt:{item.call.call_id}" for item in state.attempts
                              if item.call is not None)
        for proposal in proposals:
            if proposal.formed_at > as_of:
                return "formed_after_core_evaluation"
            if any(reference not in references for reference in proposal.source_references):
                return "source_reference_unknown"
            if any(proposal.formed_at < turn_times[reference]
                   for reference in proposal.source_references if reference in turn_times):
                return "formed_before_source"
            if proposal.kind is MemoryKind.RELATIONSHIP:
                people = [turns[reference] for reference in proposal.source_references
                          if reference in turns]
                if not people or any(person_id != proposal.person_id for person_id in people):
                    return "relationship_person_mismatch"
        return None

    @staticmethod
    def _memory_query_is_authorized(conversation: ConversationSnapshot,
                                    query: MemoryQuery) -> bool:
        if query.person_id is None:
            return True
        user_turns = [item for item in conversation.turns
                      if item.origin.value != "alx_response"]
        return bool(user_turns) and user_turns[-1].person_id == query.person_id

    @staticmethod
    def _call_id_exists(state: GoalState, call_id: str) -> bool:
        return any((item.call is not None and item.call.call_id == call_id)
                   or (item.call is None and item.result is not None
                       and item.result.call_id == call_id) for item in state.attempts)

    @staticmethod
    def _has_pending_dispatch(state: GoalState) -> bool:
        return any(item.disposition is CapabilityAttemptDisposition.PENDING
                   for item in state.attempts)

    @staticmethod
    def _validate_step_budget(step_budget: int) -> None:
        if step_budget <= 0:
            raise ValueError("step_budget must be positive")
