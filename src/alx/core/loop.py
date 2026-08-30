"""The only iterative AL/X reasoning authority."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
import logging
from uuid import uuid4

from alx.contracts import (
    AgentDecision, Approval, ApprovalLifecycle, CapabilityAttempt, CapabilityAttemptDisposition,
    CapabilityDefinition, CapabilityDispatch, CapabilityResult,
    ConversationOrigin,
    CapabilityResultState, ConversationSnapshot,
    DurableGoalStore, DurableMemoryStore, GoalMutationKind, GoalProposal,
    GoalSnapshot, GoalState, GoalStatus, GoalStopReason, MemoryKind,
    MemoryProposal, MemoryQuery, MemorySnapshot, Objective, ReasoningContext,
    ReasoningProvider, SideEffect,
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
                 identifier_factory: Callable[[], str] | None = None,
                 approval_ttl_seconds: int | None = None) -> None:
        self._store = store
        self._reasoner = reasoner
        self._dispatch = dispatch
        self._capabilities = tuple(capabilities)
        self._memory_store = memory_store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._identifier_factory = identifier_factory or (lambda: str(uuid4()))
        if approval_ttl_seconds is not None and approval_ttl_seconds <= 0:
            raise ValueError("approval_ttl_seconds must be positive")
        self._approval_ttl_seconds = approval_ttl_seconds

    def process(self, conversation: ConversationSnapshot, goal_id: str | None,
                retention_until: datetime, step_budget: int,
                trigger_event_id: str | None = None) -> CoreOutcome:
        """Reason over one durable conversation and its optional attached goal."""
        self._validate_step_budget(step_budget)
        snapshot = None if goal_id is None else self._store.load(goal_id)
        if snapshot is not None and snapshot.conversation_id != conversation.conversation_id:
            return CoreOutcome(CoreState.ERROR, snapshot, reason="goal_conversation_mismatch")
        if snapshot is not None and self._has_pending_dispatch(snapshot.state):
            # A dispatch that never returned means the process stopped between
            # the durable checkpoint and the result. Refusing here would wedge
            # the goal permanently, so the attempt is closed with an explicitly
            # unknown outcome. It is never retried automatically: the external
            # action may already have taken effect, and only the Core may
            # decide, from this evidence, whether to verify or ask.
            snapshot = self._close_interrupted_dispatch(snapshot)
        if snapshot is not None and not self._flush_pending_memory_batches(snapshot.state.goal_id):
            return CoreOutcome(CoreState.ERROR, snapshot, reason="memory_persistence_error")

        retrieved_memories: tuple[MemorySnapshot, ...] = ()
        transient_attempts: tuple[CapabilityAttempt, ...] = ()
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
                    active_goal=None if snapshot is None else snapshot.state,
                    turns=conversation.turns,
                    capabilities=self._capabilities,
                    memories=retrieved_memories,
                    events=conversation.events,
                    transient_attempts=transient_attempts,
                    conversation_id=conversation.conversation_id,
                    trigger_event_id=trigger_event_id,
                ))
            except Exception as error:
                LOGGER.info("Reasoner decision rejected: %s: %s", type(error).__name__, error)
                return CoreOutcome(CoreState.ERROR, snapshot, reason="reasoner_error")
            decision = self._without_redundant_approval(decision)

            previous = snapshot
            candidate, proposal_error = self._reduce_goal_proposal(
                snapshot, decision.goal_proposal, conversation,
            )
            if proposal_error is None and decision.approval_proposal is not None:
                approval_error = self._approval_proposal_error(
                    conversation, candidate, decision
                )
                if approval_error is not None:
                    LOGGER.info("Approval proposal rejected: %s", approval_error)
                    return CoreOutcome(
                        CoreState.ERROR,
                        snapshot,
                        reason="approval_proposal_invalid",
                    )
                assert candidate is not None
                proposed = decision.approval_proposal
                candidate = replace(
                    candidate,
                    approvals=(
                        *candidate.approvals,
                        Approval(
                            proposed.approval_id,
                            proposed.scope,
                            ApprovalLifecycle.GRANTED,
                            # A stale authorisation must not act later.
                            None if self._approval_ttl_seconds is None
                            else now + timedelta(seconds=self._approval_ttl_seconds),
                        ),
                    ),
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
            if proposal_error is None and (
                decision.goal_proposal is not None
                or decision.approval_proposal is not None
            ):
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
                definition = next(
                    (item for item in self._capabilities
                     if item.capability_id == decision.call.capability_id),
                    None,
                )
                if definition is None or definition.side_effect not in (
                    SideEffect.NONE,
                    SideEffect.ATTENTION_STATE,
                ):
                    return CoreOutcome(CoreState.ERROR, snapshot, reason="active_goal_required")
                if any(item.call is not None and item.call.call_id == decision.call.call_id
                       for item in transient_attempts):
                    return CoreOutcome(CoreState.ERROR, snapshot, reason="call_id_reused")
                snapshot, committed = self._commit_memories(
                    snapshot, decision.memory_proposals, retention_until)
                if not committed:
                    return CoreOutcome(CoreState.ERROR, snapshot, reason="memory_persistence_error")
                try:
                    attempt = self._dispatch(decision.call, None)
                except Exception:
                    return CoreOutcome(CoreState.ERROR, snapshot, reason="dispatch_error")
                if (attempt.call != decision.call
                        or attempt.disposition is CapabilityAttemptDisposition.PENDING):
                    return CoreOutcome(CoreState.ERROR, snapshot, reason="attempt_invalid")
                transient_attempts = (*transient_attempts, attempt)
                continue
            if self._call_id_exists(snapshot.state, decision.call.call_id):
                return CoreOutcome(CoreState.ERROR, snapshot, reason="call_id_reused")
            if self._repeats_rejected_call(snapshot.state, decision.call):
                return CoreOutcome(
                    CoreState.ERROR,
                    snapshot,
                    reason="repeated_rejected_call",
                )
            authority_state = snapshot.state
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
                # The durable checkpoint claims the approval before external work
                # begins, preventing a restart from dispatching it twice. The safety
                # gate must evaluate the immutable pre-claim authority snapshot,
                # where the exact approval is still GRANTED.
                attempt = self._dispatch(decision.call, authority_state)
            except Exception:
                return CoreOutcome(CoreState.ERROR, snapshot, reason="dispatch_error")
            if attempt.call != decision.call:
                return CoreOutcome(CoreState.ERROR, snapshot, reason="attempt_invalid")
            snapshot = self._finalize_dispatch(snapshot, attempt)
        return CoreOutcome(CoreState.CHECKPOINTED, snapshot, reason="budget_exhausted")

    def _close_interrupted_dispatch(self, snapshot: GoalSnapshot) -> GoalSnapshot:
        """Resolve an interrupted dispatch as an unknown outcome, once."""
        pending = tuple(item for item in snapshot.state.attempts
                        if item.disposition is CapabilityAttemptDisposition.PENDING)
        if len(pending) != 1 or pending[0].call is None:
            return snapshot
        call = pending[0].call
        closed = CapabilityAttempt(
            call,
            CapabilityAttemptDisposition.BROKER_FAILURE,
            True,
            CapabilityResult(
                call.call_id,
                call.capability_id,
                CapabilityResultState.FAILED,
                failure={"code": "dispatch_interrupted"},
            ),
            "dispatch_interrupted",
        )
        LOGGER.info("Closing interrupted dispatch for %s", call.capability_id)
        return self._finalize_dispatch(snapshot, closed)

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

    # Text AL/X composed herself and is about to send outward must already have
    # reached Friedl in something she said. This is a property of the artifact,
    # not an ordering rule: she may draft, ask, argue, or change her mind in any
    # order, and needs no permission to speak. She simply cannot transmit words
    # he never heard, which is what stops her reporting one message and sending
    # another.
    _AUTHORED_TEXT_ARGUMENTS = frozenset({"body", "body_text"})

    @staticmethod
    def _unheard_authored_text(
        conversation: ConversationSnapshot, call: CapabilityCall
    ) -> bool:
        """Report whether an outbound call carries text Friedl has not heard."""
        authored = [
            value
            for name, value in call.arguments.items()
            if name in CoreAgent._AUTHORED_TEXT_ARGUMENTS
            and isinstance(value, str)
            and value.strip()
        ]
        if not authored:
            return False
        spoken = " ".join(
            " ".join(item.content.split())
            for item in conversation.turns
            if item.origin is ConversationOrigin.ALX_RESPONSE
        )
        return any(
            " ".join(item.split()) not in spoken for item in authored
        )

    @staticmethod
    def _approval_proposal_error(conversation, state, decision) -> str | None:
        proposal = decision.approval_proposal
        call = decision.call
        if proposal is None:
            return None
        if state is None or call is None:
            return "active_goal_required"
        if proposal.approval_id != call.approval_id:
            return "approval_call_id_mismatch"
        if not proposal.scope.matches(call):
            return "approval_scope_mismatch"
        if any(item.approval_id == proposal.approval_id for item in state.approvals):
            return "approval_id_reused"
        if not conversation.turns:
            return "approval_source_missing"
        source = conversation.turns[-1]
        if (
            source.person_id is None
            or proposal.source_reference != f"turn:{source.turn_id}"
        ):
            return "approval_source_not_latest_person_turn"
        if CoreAgent._unheard_authored_text(conversation, call):
            return "approval_covers_unheard_text"
        return None

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

    # Transient material is shown to the model so it can speak about a message,
    # but it was promised never to enter durable state. A model-authored record
    # can otherwise carry the body across that boundary by quoting it, so any
    # durable text reproducing a substantial run of transient content is
    # refused. This checks content, not provenance.
    # A quoted run this long is treated as reproduction. It is deliberately
    # short, because a leak is not always a long block: a one-line code or
    # account number is the most sensitive thing a message can carry.
    _TRANSIENT_QUOTE_CHARACTERS = 24

    @classmethod
    def _transient_texts(cls, conversation: ConversationSnapshot) -> tuple[str, ...]:
        collected: list[str] = []

        def walk(value: object) -> None:
            if isinstance(value, str):
                # Every transient string is a source, however short. A brief
                # body is not less private than a long one.
                if value.strip():
                    collected.append(value)
                return
            if isinstance(value, Mapping):
                for nested in value.values():
                    walk(nested)
                return
            if isinstance(value, (tuple, list)):
                for nested in value:
                    walk(nested)

        for event in conversation.events:
            walk(event.transient_data)
        return tuple(collected)

    @classmethod
    def _durable_texts(cls, value: object) -> list[str]:
        found: list[str] = []
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, Mapping):
            for nested in value.values():
                found.extend(cls._durable_texts(nested))
        elif isinstance(value, (tuple, list)):
            for nested in value:
                found.extend(cls._durable_texts(nested))
        return found

    @classmethod
    def _reproduces_transient_content(
        cls, conversation: ConversationSnapshot, values: object
    ) -> bool:
        """Report whether durable text reproduces transient content.

        Reproduction is judged per durable string and by content, not by block
        size. A short transient body copied whole is caught, and so is a long
        body divided among several short records, because each fragment is
        still compared against the whole source.
        """
        sources = [" ".join(item.split()) for item in cls._transient_texts(conversation)]
        sources = [item for item in sources if item]
        if not sources:
            return False
        window = cls._TRANSIENT_QUOTE_CHARACTERS
        for candidate in cls._durable_texts(values):
            normalised = " ".join(candidate.split())
            if not normalised:
                continue
            for source in sources:
                # A short source copied in full is reproduction in full.
                if len(source) <= window:
                    if source in normalised:
                        return True
                    continue
                # Otherwise any sufficiently long shared run is reproduction,
                # whichever side the fragment came from.
                shortest = min(len(normalised), len(source))
                if shortest < window:
                    continue
                for start in range(0, len(normalised) - window + 1):
                    if normalised[start:start + window] in source:
                        return True
        return False

    @staticmethod
    def _evidence_grounding_error(conversation: ConversationSnapshot,
                                  state: GoalState, existing: tuple,
                                  proposed: tuple) -> str | None:
        known = {f"turn:{item.turn_id}" for item in conversation.turns}
        known.update(f"event:{item.event_id}" for item in conversation.events)
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
            if CoreAgent._reproduces_transient_content(conversation, item.attributes):
                return "evidence_reproduces_transient_content"
            known.add(f"evidence:{item.evidence_id}")
        return None

    # A definite pre-effect failure returns the approval, because the external
    # action provably did not happen and Friedl already authorised this exact
    # action. Anything that reached the implementation and could have taken
    # effect stays consumed, so an ambiguous outcome can never be retried
    # automatically against production.
    _PRE_EFFECT_FAILURE_CODES = frozenset({
        "capability_unknown",
        "implementation_missing",
        "input_invalid",
        "executor_error",
    })

    @classmethod
    def _approval_is_spent(cls, attempt: CapabilityAttempt) -> bool:
        """Report whether an approval must not be reused after this attempt."""
        if not attempt.implementation_invoked:
            return False
        if attempt.disposition is CapabilityAttemptDisposition.BROKER_FAILURE and (
            attempt.reason_code in cls._PRE_EFFECT_FAILURE_CODES
        ):
            return False
        return True

    def _finalize_dispatch(self, snapshot: GoalSnapshot,
                           attempt: CapabilityAttempt) -> GoalSnapshot:
        assert attempt.call is not None
        consumed = self._approval_is_spent(attempt)
        approvals = tuple(
            replace(item, lifecycle=(ApprovalLifecycle.CONSUMED
                                     if consumed
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
        event_times = {
            f"event:{item.event_id}": item.occurred_at for item in conversation.events
        }
        references = {*turns, *event_times}
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
            if any(proposal.formed_at < event_times[reference]
                   for reference in proposal.source_references if reference in event_times):
                return "formed_before_source"
            if proposal.kind is MemoryKind.RELATIONSHIP:
                people = [turns[reference] for reference in proposal.source_references
                          if reference in turns]
                if not people or any(person_id != proposal.person_id for person_id in people):
                    return "relationship_person_mismatch"
            if CoreAgent._reproduces_transient_content(
                conversation, (proposal.content, proposal.meaning)
            ):
                return "memory_reproduces_transient_content"
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
    def _repeats_rejected_call(state: GoalState, call: CapabilityCall) -> bool:
        """Stop deterministic safety/input rejections from becoming model loops.

        The identity of a repeat is the capability and its arguments. A new
        call or approval identifier does not make a refused action different,
        so retrying the same refused action with fresh identifiers is still a
        loop and is stopped here.
        """
        return any(
            item.call is not None
            and item.disposition is CapabilityAttemptDisposition.REJECTED
            and item.call.capability_id == call.capability_id
            and item.call.arguments == call.arguments
            for item in state.attempts
        )

    @staticmethod
    def _has_pending_dispatch(state: GoalState) -> bool:
        return any(item.disposition is CapabilityAttemptDisposition.PENDING
                   for item in state.attempts)

    @staticmethod
    def _validate_step_budget(step_budget: int) -> None:
        if step_budget <= 0:
            raise ValueError("step_budget must be positive")

    def _without_redundant_approval(self, decision: AgentDecision) -> AgentDecision:
        call = decision.call
        if call is None:
            return decision
        definition = next(
            (item for item in self._capabilities
             if item.capability_id == call.capability_id),
            None,
        )
        if definition is None or definition.side_effect is SideEffect.EFFECTFUL:
            return decision
        if decision.approval_proposal is None and call.approval_id is None:
            return decision
        LOGGER.info(
            "Ignoring redundant approval metadata for %s capability %s",
            definition.side_effect.value,
            definition.capability_id,
        )
        return replace(
            decision,
            call=replace(call, approval_id=None),
            approval_proposal=None,
        )
