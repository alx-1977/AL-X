"""The only iterative AL/X reasoning authority."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
import hashlib
import json
import logging
from uuid import uuid4

from alx.contracts import (
    AgentDecision, Approval, ApprovalLifecycle, AutonomousReasoningDisabled,
    DecisionValidationError,
    AutonomousRequestUnbounded,
    CapabilityAttempt, CapabilityAttemptDisposition,
    CapabilityCall, CapabilityDefinition, CapabilityDispatch, CapabilityResult,
    ConversationOrigin,
    CapabilityResultState, CognitionOrigin, ConversationSnapshot, ConversationTurn,
    DurableGoalStore, DurableMemoryStore, GoalMutationKind, GoalProposal,
    ExecutionPlan, MemoryIdentityConflict, PLAN_TERMINAL, PlanAnnouncement, PlanDispatch,
    PlanOperation, PlanStatus, PlanUpdate,
    GoalSnapshot, GoalState, GoalStatus, GoalStopReason, GoalSummary, MemoryKind,
    MemoryProposal, MemoryQuery, MemorySnapshot, Objective, ReasoningContext,
    ReasoningProvider, SideEffect,
    ContentOrigin, ContentProvenance, RetentionPolicy,
    history_evidence_ids,
)

from alx.core.plan_results import (
    classify_planned_result, defer_plan, finish_plan, plan_invalidation_facts,
    raise_attention, reduce_plan, resume_plan,
)

LOGGER = logging.getLogger(__name__)

# How long a planned step refused by the execution budget waits before it is
# tried again. Mechanical: waking a paid reasoner because spending stopped
# would defeat the point.
PLAN_BUDGET_RETRY_SECONDS = 300

# Why an autonomous turn did not happen when its request would not fit.
INPUT_BOUND_EXCEEDED = "input_bound_exceeded"

_RUN_CODING_TASK = "run_coding_task"
# Repeated failures within one open failure episode. An episode closes when a
# coding job succeeds or when AL/X dispatches a correction of a recorded
# failure; it is not a goal-wide allowance.
_MAX_FAILED_CODING_EXECUTIONS = 2
# Corrections dispatched against failures with the same failure signature.
# Distinct diagnoses of an unchanged failure are bounded too.
_MAX_CORRECTIONS_PER_FAILURE = 2
_CORRECTIVE_ACTION = "corrective_action"
_MAX_INTERRUPTED_CODING_EXECUTIONS_PER_JOB = 4
_MAX_PLANNING_CODING_FAILURES = 2
# A job that implemented but whose required verification only the request's
# own constraints blocked. Recoverable by a changed plan, so it does not spend
# the implementation allowance, but it has its own bound so it cannot loop.
_MAX_REQUEST_CONFLICT_CODING_EXECUTIONS = 2


_CODING_CORRECTION_REFUSALS = frozenset({
    "coding_correction_unanchored",
    "coding_correction_repeated",
    "coding_correction_exhausted",
})


class CoreState(str, Enum):
    RESPONDED = "responded"
    FINISHED_SILENTLY = "finished_silently"
    CHECKPOINTED = "checkpointed"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class CoreOutcome:
    state: CoreState
    snapshot: GoalSnapshot | None = None
    response: str | None = None
    reason: str | None = None
    response_provenance: ContentProvenance | None = None
    # Whether the memories this turn proposed actually persisted. A response
    # is no longer discarded because a supporting memory clashed, so the
    # durable record has to say plainly that the write did not happen.
    # None means nothing was proposed or everything proposed was stored.
    memory_state: str | None = None
    # The fixed turn id of a reply that announces a finish or cancel, held
    # on the plan until it is stored. None: an ordinary reply, any id.
    response_turn_id: str | None = None

    def __post_init__(self) -> None:
        if self.state is CoreState.RESPONDED and (
            not isinstance(self.response, str) or not self.response.strip()
        ):
            raise ValueError("responded outcomes require nonblank response text")


@dataclass(frozen=True, slots=True)
class PlannedDispatch:
    """One checkpointed planned step, handed to a background worker."""

    goal_id: str
    conversation_id: str
    plan_id: str
    call: CapabilityCall
    authority: GoalState
    # Set for an evidence read: the call whose result it re-observes, and the
    # attention it is for. Such a job never moves the plan.
    evidence_for: str | None = None
    attention_seq: int = 0


# The whole conversation is stored and never rewritten, but sending all of it
# on every call made each reasoning turn slower than the last: at 71 turns the
# provider took 35s, then 78s, then 154s before returning a blank response.
# The blueprint asks for goal-relevant context rather than the entire history,
# and forbids silently truncating an active goal. So the projection is
# deterministic and goal-preserving: the recent thread, plus every older turn
# the active work still cites. Nothing is deleted; older casual conversation
# simply has to be retrieved rather than being silently present.
REASONING_TURN_WINDOW = 12
# How many unfinished goals one reasoning call may be shown. Awareness of open
# work must not grow with the history: without a bound, every conversation
# would eventually carry every goal AL/X has ever left unfinished. The cap is
# on the final projection, not on each source that feeds it.
UNFINISHED_GOAL_CANDIDATES = 10
# Plans that need AL/X, listed beside those candidates in their own bound so
# that neither can crowd the other out.
PLAN_ATTENTION_CANDIDATES = 5


def project_turns_for_reasoning(
    turns: Sequence[ConversationTurn],
    state: GoalState | None,
    window: int = REASONING_TURN_WINDOW,
) -> tuple[ConversationTurn, ...]:
    """The turns one reasoning call sees, in their original order.

    Keeps the last `window` turns, and any older turn the active goal still
    depends on: its objective's source, evidence sources, decisions,
    corrections and approvals. A goal therefore never loses the turn it came
    from, however long the conversation grows.
    """
    ordered = tuple(turns)
    if window <= 0 or len(ordered) <= window:
        return ordered
    referenced = _referenced_turn_ids(state)
    recent = {item.turn_id for item in ordered[-window:]}
    return tuple(
        item
        for item in ordered
        if item.turn_id in recent or item.turn_id in referenced
    )


def _referenced_turn_ids(state: GoalState | None) -> frozenset[str]:
    """Every turn id the active goal still points at."""
    if state is None:
        return frozenset()
    references: set[str] = set()

    def note(value: object) -> None:
        if isinstance(value, str) and value.startswith("turn:"):
            references.add(value[len("turn:") :])

    note(getattr(state.objective, "source_reference", None))
    for item in state.evidence:
        for reference in item.source_references:
            note(reference)
    for group in (state.decisions, state.corrections):
        for item in group:
            note(getattr(item, "source_reference", None))
    for item in state.approvals:
        note(getattr(item, "source_reference", None))
    return frozenset(references)


class CoreAgent:
    def __init__(self, store: DurableGoalStore, reasoner: ReasoningProvider,
                 dispatch: CapabilityDispatch,
                 capabilities: tuple[CapabilityDefinition, ...],
                 memory_store: DurableMemoryStore | None = None,
                 clock: Callable[[], datetime] | None = None,
                 identifier_factory: Callable[[], str] | None = None,
                 approval_ttl_seconds: int | None = None,
                 budget_check: Callable[[str], None] | None = None,
                 turn_bound_capabilities: frozenset[str] = frozenset(),
                 approval_free_capabilities: frozenset[str] = frozenset(),
                 open_thoughts: Callable[[], tuple] | None = None,
                 pending_revisits: Callable[[], tuple] | None = None,
                 open_notebook_threads: Callable[[], tuple] | None = None,
                 undelivered_responses: Callable[[], tuple] | None = None,
                 record_goal_rejection: Callable[[Mapping[str, Any]], None] | None = None,
                 plan_continuation: bool = False,
                 bind_dispatch: Callable[[str], None] | None = None) -> None:
        self._store = store
        # Whether the runtime can return a plan to her: completion, failure,
        # changed preconditions and judgment reach the Core only through a
        # plan attention occasion. Without one no plan is installed and no
        # tick dispatches a planned step. Fails closed by default.
        self._plan_continuation = plan_continuation
        # Binds the goal's conversation for a planned dispatch running on a
        # background worker, as a reasoning step's budget check binds it for
        # the turn's own calls. It checks nothing and spends nothing.
        self._bind_dispatch = bind_dispatch or (lambda _conversation_id: None)
        # Planned dispatches a background worker in this process is running.
        # A pending attempt not listed here was interrupted by a restart.
        self._live_plan_dispatches: set[str] = set()
        # The full results an attention cites, when they arrived in this
        # process. Content may be transient by contract, so after a restart
        # only durable metadata remains and an observation is made again.
        self._plan_evidence_cache: dict[str, CapabilityAttempt] = {}
        # Evidence reads scheduled, and returned, in this process, by goal,
        # attention and evidence: each is read at most once per attention.
        self._evidence_scheduled: set[tuple[str, int, str]] = set()
        self._evidence_settled: set[tuple[str, int, str]] = set()
        # Which scheduled read each evidence dispatch is, so a read stopped
        # before its result was recorded can be scheduled again.
        self._evidence_reads: dict[str, tuple[str, int, str]] = {}
        self._reasoner = reasoner
        # Mechanical record of a refused goal proposal, for diagnosis. The
        # cited references, mutation and response dependence: enough to diagnose
        # why it failed without recording hidden reasoning. On
        # 2026-09-04 a live rejection could not be diagnosed because the
        # proposal was never recorded anywhere.
        self._record_goal_rejection = record_goal_rejection or (lambda _record: None)
        # One transient, provenance-bound capture for inspecting the failed
        # decision. Payload text never reaches the persistent diagnostic sink.
        self._last_goal_rejection: tuple[dict[str, Any], ContentProvenance] | None = None
        self._dispatch = dispatch
        self._capabilities = tuple(capabilities)
        self._memory_store = memory_store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._identifier_factory = identifier_factory or (lambda: str(uuid4()))
        if approval_ttl_seconds is not None and approval_ttl_seconds <= 0:
            raise ValueError("approval_ttl_seconds must be positive")
        self._approval_ttl_seconds = approval_ttl_seconds
        # Raises before another reasoning call when a routine task has run
        # away, so the ceiling prevents spend rather than reporting it.
        self._budget_check = budget_check or (lambda _task_id: None)
        # Capabilities whose authority policy requires an approval grounded in
        # Friedl's latest turn. One such instruction authorises one such
        # action, so these are dispatched at most once per turn. Supplied from
        # the policies already built at composition rather than named here: the
        # rule belongs to the authority the policy declares, and a list of
        # capability identifiers in the Core would be a second place to keep it
        # right. Capabilities that merely accept an approval are not bound,
        # because for them a repeat is ordinary work rather than a second
        # authorised action.
        self._turn_bound_capabilities = frozenset(turn_bound_capabilities)
        # Capabilities that reach outside but whose policy requires no
        # approval. Supplied from the same policies as the turn-bound set, so
        # the Core can tell "needs Friedl's word" from "needs only permission"
        # instead of assuming every effectful call needs an approval. Empty
        # means the composition did not say, and nothing is assumed.
        self._approval_free_capabilities = frozenset(approval_free_capabilities)
        # Thoughts AL/X still holds, supplied by the one continuity store. The
        # Core asks for them; it never reaches the store itself, and the same
        # call is made for every turn whatever its origin.
        self._open_thoughts = open_thoughts or (lambda: ())
        # The later occasions she has asked for and not yet had, from the same
        # continuity store and the same for every turn. Without them a revisit
        # she made in one conversation was invisible from every other, and the
        # only way to learn it was obsolete was to be woken by it.
        self._pending_revisits = pending_revisits or (lambda: ())
        # Her open enquiries, from the one notebook store, bounded and
        # content-free. Supplied identically on every turn: a context
        # assembled differently when nobody is watching would be a second
        # builder deciding what she is like unprompted.
        self._open_notebook_threads = open_notebook_threads or (lambda: ())
        # Occasions whose response had nowhere to go. Supplied by the one
        # opportunity ledger; the Core is shown that it happened and nothing
        # deterministic decides whether it still matters.
        self._undelivered_responses = undelivered_responses or (lambda: ())

    def process(self, conversation: ConversationSnapshot, retention_until: datetime,
                step_budget: int, trigger_event_id: str | None = None,
                origin: CognitionOrigin = CognitionOrigin.PERSON_TURN,
                resume_plan_goal_id: str | None = None) -> CoreOutcome:
        """Reason over one durable conversation and whichever of its goals AL/X selects.

        No goal is attached in advance. Every unfinished goal of the
        conversation reaches the Core as a compact summary; the Core says
        which one the input belongs to, starts a new one, or works without
        one. Deterministic code only checks that a selected goal exists and
        loads its full state.
        """
        self._validate_step_budget(step_budget)
        conversation_id = conversation.conversation_id
        # Recovery is deliberately unbounded and unscoped, unlike the candidate
        # projection below. An interrupted dispatch or an unflushed memory
        # batch is a fact about durable state, not a thing to be shown to
        # reasoning, and leaving one unrepaired because it belongs to another
        # conversation would wedge that goal permanently.
        for summary in self._store.list_unfinished():
            if summary.has_pending_dispatch:
                # A dispatch that never returned means the process stopped
                # between the durable checkpoint and the result. Refusing
                # would wedge the goal permanently, so the attempt is closed
                # with an explicitly unknown outcome. It is never retried
                # automatically: the external action may already have taken
                # effect, and only the Core may decide, from this evidence,
                # whether to verify or ask.
                self._close_interrupted_dispatch(self._store.load(summary.goal_id))
            if not self._flush_pending_memory_batches(summary.goal_id):
                return CoreOutcome(CoreState.ERROR, None, reason="memory_persistence_error")

        snapshot: GoalSnapshot | None = None
        # Results a plan's attention asks her to judge, shown to every step.
        plan_evidence: tuple[CapabilityAttempt, ...] = ()
        if resume_plan_goal_id is not None:
            # A plan attention occasion. It names its goal, so the goal is the
            # one the turn works under; whether it still needs her is read
            # from the plan, never from the occasion.
            snapshot = self._store.load(resume_plan_goal_id)
            if snapshot.conversation_id != conversation_id:
                return CoreOutcome(CoreState.ERROR, snapshot, reason="plan_conversation_changed")
            plan = snapshot.state.execution_plan
            if plan is None or plan.status is not PlanStatus.NEEDS_CORE:
                return CoreOutcome(CoreState.CHECKPOINTED, snapshot,
                                   reason="plan_attention_resolved")
            snapshot, plan_evidence = self._plan_evidence(snapshot)
        retrieved_memories: tuple[MemorySnapshot, ...] = ()
        # Identifier clashes seen this turn, handed to the next reasoning call
        # so she can resolve them. Cleared once she stops proposing the
        # conflicting write, so a resolved turn carries nothing forward.
        memory_conflicts: tuple[Mapping[str, Any], ...] = ()
        # Calls refused before approval this turn, each reported to her once.
        refused_calls: tuple[Mapping[str, Any], ...] = ()
        # A settled mechanical blocker is explained once. Pending external work
        # never reaches this boundary: its executor waits without reasoning.
        mechanical_blocker: str | None = None
        # One-shot notice that a call-less decision would have ended the turn
        # while remaining work was still immediately executable. Shown on the
        # next reasoning step, then cleared.
        continuation_notices: tuple[Mapping[str, Any], ...] = ()
        refused_goal_selections: tuple[Mapping[str, Any], ...] = ()
        continuation_notice_issued = False
        # Capabilities this turn has already dispatched under an approval.
        # One instruction from Friedl authorises one such action, and the
        # single-use approval identifier does not enforce that on its own: the
        # Core can propose a *fresh* approval in a later step of the same turn,
        # citing the same turn again, and one instruction became two /review
        # comments eleven seconds apart. What is spent is the turn, not the
        # identifier, so it is recorded for the turn rather than in the goal.
        # It is deliberately local: durable state outlives the instruction, and
        # a check against it would refuse the next turn's legitimate request.
        approved_dispatches: set[str] = set()
        # An answer she had already finished when a memory identifier clashed.
        # Held so that running out of steps mid-resolution delivers her words
        # instead of discarding them; memory_state stays truthful that the
        # supporting memory did not persist.
        conflict_response: str | None = None
        conflict_provenance: ContentProvenance | None = None
        conflict_silent = False
        transient_attempts: tuple[CapabilityAttempt, ...] = plan_evidence
        memory_query_ids: set[str] = set()
        # Loading a goal's full state is one useful inspection this turn.
        # A later choice may inspect a different offered goal, but never one
        # already inspected. Candidate projection and the existing step budget
        # bound traversal without requiring an unrelated durable mutation.
        selected_goals: set[str] = set()
        prior_goal_provenance: tuple[ContentProvenance, ...] = ()
        # Whether this turn has already had its one correction of a decision
        # the deterministic validator rejected.
        decision_corrected = False
        for step_index in range(step_budget):
            try:
                now = self._clock()
                if now.tzinfo is None or now.utcoffset() is None:
                    raise ValueError("Core clock must be timezone-aware")
            except Exception:
                return CoreOutcome(CoreState.ERROR, snapshot, reason="clock_error")
            if snapshot is not None:
                # A newly created goal is already known in full this turn.
                selected_goals.add(snapshot.state.goal_id)
            summaries = self._selectable_goals(conversation_id, snapshot)
            decision_provenance = self._derived_provenance(
                now,
                conversation,
                snapshot,
                retrieved_memories,
                transient_attempts, prior_goal_provenance,
            )
            try:
                self._budget_check(conversation_id)
            except Exception as error:
                LOGGER.warning("Reasoning stopped by execution budget: %s", error)
                return CoreOutcome(
                    CoreState.CHECKPOINTED, snapshot, reason="budget_exceeded"
                )
            try:
                reasoning_context = ReasoningContext(
                    active_goal=None if snapshot is None else snapshot.state,
                    turns=project_turns_for_reasoning(
                        conversation.turns,
                        None if snapshot is None else snapshot.state,
                    ),
                    capabilities=self._capabilities,
                    memories=retrieved_memories,
                    events=conversation.events,
                    transient_attempts=transient_attempts,
                    conversation_id=conversation_id,
                    trigger_event_id=trigger_event_id,
                    unfinished_goals=summaries,
                    origin=origin,
                    carried_thoughts=self._open_thoughts(),
                    pending_revisits=self._pending_revisits(),
                    open_notebook_threads=self._open_notebook_threads(),
                    undelivered_responses=self._undelivered_responses(),
                    memory_conflicts=memory_conflicts,
                    refused_calls=refused_calls,
                    continuation_notices=continuation_notices,
                    refused_goal_selections=refused_goal_selections,
                )
                rendered_plan = self._rendered_plan(reasoning_context.active_goal)
                decision = self._reasoner.decide(reasoning_context)
            except AutonomousReasoningDisabled as error:
                LOGGER.info("Autonomous reasoning is disabled: %s", error)
                return CoreOutcome(
                    CoreState.FINISHED_SILENTLY,
                    snapshot,
                    reason="autonomous_reasoning_disabled",
                )
            except AutonomousRequestUnbounded as error:
                # Not a failed turn and not a finished one: the occasion is
                # known and valid, and cannot be carried at this bound. Named,
                # so the runner can hold it durably instead of offering the
                # same oversized request again on every tick.
                LOGGER.info("Autonomous request over its input bound: %s", error)
                return CoreOutcome(
                    CoreState.ERROR, snapshot, reason=INPUT_BOUND_EXCEEDED
                )
            except DecisionValidationError as error:
                # Her decision broke a deterministic rule, such as a plan
                # condition naming no result field. Nothing was applied. The
                # exact reason is new evidence, so she may correct it once;
                # the correction is validated exactly as the first was.
                LOGGER.info("Core decision rejected by validation: %s", error.reason)
                if not decision_corrected and step_index + 1 < step_budget:
                    decision_corrected = True
                    refused_calls = (*refused_calls, {
                        "reason": "decision_rejected", "subject": error.reason,
                    })
                    continue
                # Rejected again, or no step left: there is no third attempt.
                if origin is CognitionOrigin.PERSON_TURN:
                    # A person is never left in silence. One response-only
                    # step, which can only speak, says what could not start.
                    return self._respond_to_terminal_blocker(
                        conversation_id, conversation, snapshot, reasoning_context,
                        transient_attempts, "decision_rejected", decision_provenance,
                        (*refused_calls, {"reason": "decision_rejected",
                                          "subject": error.reason}),
                        park=False,
                    )
                return CoreOutcome(CoreState.ERROR, snapshot, reason="decision_rejected")
            except Exception as error:
                LOGGER.info("Reasoner decision rejected: %s: %s", type(error).__name__, error)
                return CoreOutcome(CoreState.ERROR, snapshot, reason="reasoner_error")
            if mechanical_blocker is not None and (
                decision.call is not None or decision.memory_query is not None
            ):
                if snapshot is not None:
                    snapshot = self._park_unfinished_goal(snapshot, decision_provenance)
                return CoreOutcome(CoreState.CHECKPOINTED, snapshot, reason=mechanical_blocker)
            continuation_notices = ()
            deferred_selection: str | None = None
            if decision.goal_id is not None:
                selection_error = self._goal_selection_error(
                    decision, snapshot, summaries, selected_goals
                )
                if selection_error is not None:
                    LOGGER.info(
                        "Goal selection rejected: %s current=%s requested=%s "
                        "selected=%s response_present=%s response_requires_goal_commit=%s",
                        selection_error,
                        None if snapshot is None else snapshot.state.goal_id,
                        decision.goal_id, tuple(selected_goals),
                        decision.response is not None, decision.response_requires_goal_commit,
                    )
                    # Selecting a goal this conversation does not offer is a
                    # correctable slip, not the end of the conversation. It
                    # read nothing, changed nothing and dispatched nothing, so
                    # the state she would reason from next is the state she
                    # reasoned from just now, plus the fact that the identifier
                    # is unavailable. Killing the turn instead cost a person
                    # their whole voice session for a wrong identifier on
                    # 2026-09-10, with their actual request never attempted.
                    #
                    # Only an unknown identifier is corrected here. The other
                    # selection errors are budget rules whose state a further
                    # reasoning step cannot change, so they still stop.
                    if (selection_error == "goal_selection_unknown"
                            and not refused_goal_selections):
                        refused_goal_selections = (*refused_goal_selections, {
                            "goal_id": decision.goal_id,
                            "reason": selection_error,
                            "available_goal_ids": [
                                item.goal_id for item in summaries
                            ],
                        })
                        continue
                    # Told once and selected an unavailable goal again, or an
                    # error correction cannot help: reasoning further against
                    # an unchanged list is the runaway this stops.
                    if selection_error == "goal_selection_unknown":
                        return CoreOutcome(CoreState.ERROR, snapshot, reason=selection_error)
                    if decision.response is None or decision.response_requires_goal_commit:
                        return CoreOutcome(CoreState.CHECKPOINTED, snapshot, reason=selection_error)
                    # The refused selection authorises no mutation or action.
                    # An independent answer still uses canonical memory checks
                    # and delivery, without stale-work continuation buying a retry.
                    deferred_selection = selection_error
                    decision = replace(decision, goal_id=None, goal_proposal=None)
                if decision.goal_id is not None and (
                    snapshot is None or snapshot.state.goal_id != decision.goal_id
                ):
                    if snapshot is not None and snapshot.provenance is not None:
                        prior_goal_provenance = (*prior_goal_provenance, snapshot.provenance)
                    snapshot = self._store.load(decision.goal_id)
                    # A goal may be resumed from any conversation, but only one
                    # that was actually offered this turn. The check used to be
                    # that the goal belonged to this conversation, which made
                    # durable work unreachable the moment the runtime carried a
                    # different id. What it was really protecting is unchanged:
                    # reasoning may reach the candidates it was shown and no
                    # others, so a goal id invented or remembered from elsewhere
                    # still buys nothing.
                    if not any(
                        item.goal_id == decision.goal_id for item in summaries
                    ):
                        return CoreOutcome(
                            CoreState.ERROR, None, reason="goal_selection_unknown"
                        )
                    selected_goals.add(decision.goal_id)
                    snapshot, selected_evidence = self._plan_evidence(snapshot)
                    transient_attempts = (*transient_attempts, *(
                        item for item in selected_evidence if item not in transient_attempts
                    ))
                    # The selected goal is now a reasoning input, so the
                    # provenance of everything this step persists must include
                    # it. It was computed before the goal was known.
                    decision_provenance = self._derived_provenance(
                        now, conversation, snapshot, retrieved_memories,
                        transient_attempts, prior_goal_provenance,
                    )
                if decision.selects_only:
                    # Reading a goal before acting is a step, not a mutation.
                    # Anything the Core proposes alongside it is reduced,
                    # grounded and persisted by the one canonical block below,
                    # exactly as it is for every other decision. A second path
                    # here persisted an objective change before memory
                    # grounding could reject the turn.
                    if (
                        decision.goal_proposal is None
                        and not decision.memory_proposals
                    ):
                        continue
            decision = self._without_redundant_approval(decision)
            decision = replace(
                decision,
                memory_proposals=tuple(
                    replace(item, provenance=decision_provenance)
                    for item in decision.memory_proposals
                ),
            )

            previous = snapshot
            candidate, proposal_error = self._reduce_goal_proposal(
                snapshot, decision.goal_proposal, conversation, trigger_event_id,
            )
            memory_error = self._memory_proposal_grounding_error(
                conversation,
                candidate if candidate is not None else (
                    None if snapshot is None else snapshot.state
                ),
                decision.memory_proposals,
                now,
            )
            if memory_error is not None:
                LOGGER.info("Memory proposal rejected: %s", memory_error)
                # Nothing is stored for a rejected proposal, and this fires
                # before the goal is persisted, so the decision leaves no trace
                # to undo. The specific grounding fault is named so she can
                # correct that field rather than resend the same proposal.
                if self._already_refused(
                    refused_calls, "memory_proposal_invalid", memory_error
                ):
                    return CoreOutcome(
                        CoreState.ERROR, snapshot, reason="memory_proposal_invalid"
                    )
                refused_calls = (*refused_calls, {
                    "reason": "memory_proposal_invalid",
                    "subject": memory_error,
                })
                continue
            memory_conflicts = self._conflicting_memories(
                decision.memory_proposals, retention_until,
            )
            if memory_conflicts:
                # A memory identifier is a semantic claim, not a storage
                # failure.  Check it before any goal revision or pending-memory
                # batch is written, then let the Core correct the claim.
                conflict_subject = ",".join(
                    str(item["memory_id"]) for item in memory_conflicts
                )
                if self._already_refused(
                    refused_calls, "memory_identity_conflict", conflict_subject
                ):
                    return CoreOutcome(
                        CoreState.ERROR, snapshot, reason="memory_identity_conflict"
                    )
                refused_calls = (*refused_calls, {
                    "reason": "memory_identity_conflict",
                    "subject": conflict_subject,
                })
                if decision.call is None:
                    conflict_response = decision.response
                    conflict_provenance = decision_provenance
                    conflict_silent = decision.finish_silently
                continue
            if proposal_error is not None:
                LOGGER.info("Goal proposal rejected: %s", proposal_error)
                self._record_rejection(
                    conversation, decision, proposal_error, now, decision_provenance,
                )
                # A proposal's evidence is independently reducible from its
                # requested mutation.  For example, a real completed attempt
                # remains a durable fact even if the Core asks to complete the
                # goal before all criteria are supported.  The reducer returns
                # that evidence-only state with the mutation error; persist it
                # before returning the refusal to the Core.  No other rejected
                # mutation field is present in this candidate.
                if (
                    candidate is not None
                    and (previous is None or candidate != previous.state)
                ):
                    snapshot = self._persist_goal(
                        candidate, previous, conversation, retention_until,
                        decision_provenance,
                    )
                # A refused mutation is never fatal. An answer that depends on
                # the commit (or a silence chosen on the strength of it) claims
                # a state that does not exist, so it is suppressed, but the
                # conversation, the unfinished goal and the Core all remain
                # operational. The refusal goes back to her as evidence below
                # and she reasons again from the truthful state. The same
                # refusal twice checkpoints instead: nothing about the state
                # changed, so a further step would only buy the same mutation.
                commit_dependent = (
                    decision.response_requires_goal_commit or decision.finish_silently
                )
                # The reason reaches the Core, exactly as an approval or memory
                # rejection already does. It used to go only to the log and the
                # rejection record, so a proposal refused here was invisible to
                # the next step: on 2026-09-11 an update mutation offered with
                # no goal was rejected as goal_missing, the Core was told only
                # that dispatch required an active goal, and it proposed the
                # same update again. Both steps were spent and the session
                # ended without the deletion it had decided to make.
                #
                # Subject is the mutation kind rather than the capability: the
                # fault is which mutation was offered, and a later call for the
                # same capability is a different refusal.
                if (
                    (commit_dependent or decision.response is None)
                    and self._already_refused(
                        refused_calls, proposal_error, decision.goal_proposal.kind.value
                    )
                ) or (commit_dependent and step_index + 1 >= step_budget):
                    # Also when no step remains to reason again in: the turn
                    # ends on the refusal itself rather than a budget reason
                    # that would hide it.
                    return CoreOutcome(
                        CoreState.CHECKPOINTED, snapshot, reason="goal_proposal_invalid",
                    )
                refused_calls = (*refused_calls, {
                    "reason": proposal_error,
                    "subject": decision.goal_proposal.kind.value,
                    "mutation_kind": decision.goal_proposal.kind.value,
                })
                if commit_dependent:
                    continue
            # The goal a call would run under: the reduced proposal when it was
            # accepted, or the evidence-only state when its requested mutation
            # was refused.
            effective = (
                candidate if candidate is not None
                else (None if snapshot is None else snapshot.state)
            )
            if decision.call is not None:
                blocked = self._dispatch_blocked_reason(decision.call, effective)
                if blocked is not None:
                    # Eligibility is checked before any approval is recorded
                    # and before another reasoning step is bought. A dispatch
                    # that is structurally impossible in this state stays
                    # impossible on the next step, because nothing about the
                    # state changes: reasoning again from the same goal spent
                    # twenty-five paid calls on one refused deletion. The Core
                    # stops here with the reason, the approval it proposed is
                    # left unrecorded and reusable, and the next turn reasons
                    # afresh. Only the Core can say which goal the action
                    # belongs to, so nothing is repaired deterministically.
                    LOGGER.info(
                        "Dispatch blocked before approval: %s for %s",
                        blocked,
                        decision.call.capability_id,
                    )
                    # One explanation per reason and capability, not one per
                    # turn. A Core cycling through different impossible
                    # capabilities would spend the whole step budget, which is
                    # the runaway this check exists to prevent; but stopping on
                    # any earlier refusal of any kind would mean a corrected
                    # identifier slip earlier in the turn silently swallowed an
                    # unrelated dispatch refusal before she ever saw it.
                    if self._already_refused(
                        refused_calls, blocked, decision.call.capability_id
                    ):
                        # She has been told this reason for this capability and
                        # asked for it again unchanged. Reasoning further cannot
                        # make the dispatch possible, so the turn stops here as
                        # it always did.
                        return CoreOutcome(
                            CoreState.CHECKPOINTED, snapshot, reason=blocked,
                        )
                    # Telling her why is the one thing that changes the state:
                    # she can correct the grounding, ask Friedl, or explain
                    # that she cannot act. Ending the turn silently left a
                    # reasonable request unanswered on 2026-09-04.
                    refused_calls = (*refused_calls, {
                        "call_id": decision.call.call_id,
                        "capability_id": decision.call.capability_id,
                        "reason": blocked,
                        "subject": decision.call.capability_id,
                    })
                    continue
            if proposal_error is None and decision.approval_proposal is not None:
                approval_error = self._approval_proposal_error(
                    conversation, candidate, decision, approved_dispatches
                )
                if approval_error is not None:
                    # A malformed approval authorises nothing, so the action is
                    # refused and the reason returned to the Core. Ending the
                    # conversation instead would make one slip cost the whole
                    # session while changing nothing about what may be sent.
                    LOGGER.info("Approval proposal rejected: %s", approval_error)
                    refusal = CapabilityAttempt(
                        decision.call,
                        CapabilityAttemptDisposition.REJECTED,
                        False,
                        reason_code=approval_error,
                    )
                    if snapshot is not None:
                        snapshot = self._store.replace(
                            replace(
                                snapshot.state,
                                attempts=(*snapshot.state.attempts, refusal),
                            ),
                            snapshot.retention_until,
                            snapshot.revision,
                            decision_provenance,
                        )
                    # A goal proposed in this same decision has not committed
                    # yet, so there is nothing durable to append the refusal
                    # to and it used to be discarded here. She then reasoned
                    # again with no idea what had happened: sixteen refusals
                    # in one turn told her only that something was rejected.
                    # The transient channel carries it instead, exactly as the
                    # dispatch-blocked path beside this one already does.
                    # Nothing durable is created, because nothing was
                    # dispatched and no external effect occurred.
                    if self._already_refused(
                        refused_calls, approval_error, decision.call.capability_id
                    ):
                        # One explanation per reason and capability, as above.
                        # She has been told this reason and asked for the same
                        # thing again; a further step would spend the budget on
                        # the same wall.
                        return CoreOutcome(
                            CoreState.CHECKPOINTED, snapshot, reason=approval_error,
                        )
                    refused_calls = (*refused_calls, {
                        "call_id": decision.call.call_id,
                        "capability_id": decision.call.capability_id,
                        "reason": approval_error,
                        "subject": decision.call.capability_id,
                    })
                    continue
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

            if proposal_error is None and (
                decision.goal_proposal is not None
                or decision.approval_proposal is not None
            ):
                assert candidate is not None
                snapshot = self._persist_goal(
                    candidate, previous, conversation, retention_until,
                    decision_provenance,
                )

            if decision.memory_query is not None:
                if decision.memory_query.query_id in memory_query_ids:
                    if self._already_refused(
                        refused_calls,
                        "memory_query_id_reused",
                        decision.memory_query.query_id,
                    ):
                        return CoreOutcome(
                            CoreState.ERROR, snapshot,
                            reason="memory_query_id_reused",
                        )
                    LOGGER.info("Memory query rejected: memory_query_id_reused")
                    refused_calls = (*refused_calls, {
                        "reason": "memory_query_id_reused",
                        "subject": decision.memory_query.query_id,
                    })
                    continue
                if not self._memory_query_is_authorized(conversation, decision.memory_query):
                    return CoreOutcome(CoreState.ERROR, snapshot, reason="memory_query_unauthorized")
                try:
                    snapshot, committed = self._commit_memories(
                        snapshot, decision.memory_proposals, retention_until)
                except MemoryIdentityConflict:
                    memory_conflicts = self._conflicting_memories(
                        decision.memory_proposals, retention_until)
                    continue
                memory_conflicts = ()
                if not committed:
                    return CoreOutcome(CoreState.ERROR, snapshot, reason="memory_persistence_error")
                if self._memory_store is None:
                    return CoreOutcome(CoreState.ERROR, snapshot, reason="memory_store_unavailable")
                try:
                    found = self._memory_store.retrieve(decision.memory_query, now)
                except Exception:
                    return CoreOutcome(CoreState.ERROR, snapshot, reason="memory_retrieval_error")
                # Accumulate across the retrievals of one process rather than
                # replacing. A second retrieval used to discard the first, so
                # reasoning that needed two of them could only ever see the
                # later one and would re-ask for what it had already found.
                #
                # Deduplicated by memory_id alone. Two retrievals legitimately
                # overlap, and the same memory twice is the same memory; asking
                # whether two *different* memories mean the same thing would be
                # a judgement, and it is not one this code may make.
                known = {item.memory_id for item in retrieved_memories}
                retrieved_memories = (
                    *retrieved_memories,
                    *(item for item in found if item.memory_id not in known),
                )
                memory_query_ids.add(decision.memory_query.query_id)
                continue

            if decision.call is None:
                if decision.plan_update is not None:
                    plan_refusal = self._plan_update_refusal(
                        decision.plan_update, snapshot, rendered_plan,
                        proposal_error, mechanical_blocker,
                    )
                    if plan_refusal is not None:
                        # Refused before anything else in the decision is
                        # written. Her words or silence claimed the plan
                        # change, so they are not delivered; she reasons again
                        # with the refusal, once.
                        LOGGER.info("Plan update refused: %s", plan_refusal)
                        subject = f"{decision.plan_update.operation.value}:{plan_refusal}"
                        if self._already_refused(refused_calls, plan_refusal, subject):
                            if origin is CognitionOrigin.PERSON_TURN and snapshot is not None:
                                return self._respond_to_terminal_blocker(
                                    conversation_id, conversation, snapshot,
                                    reasoning_context, transient_attempts, plan_refusal,
                                    decision_provenance, refused_calls, park=False,
                                )
                            return CoreOutcome(CoreState.CHECKPOINTED, snapshot,
                                               reason=plan_refusal)
                        refused_calls = (*refused_calls, {
                            "reason": plan_refusal, "subject": subject,
                        })
                        continue
                try:
                    snapshot, committed = self._commit_memories(
                        snapshot, decision.memory_proposals, retention_until)
                except MemoryIdentityConflict:
                    # She reused an identifier that already means something
                    # else. That is hers to resolve, so the conflict goes back
                    # with the facts and the turn continues. If no step remains
                    # the answer is still delivered below, and memory_state
                    # records that nothing was stored.
                    memory_conflicts = self._conflicting_memories(
                        decision.memory_proposals, retention_until)
                    conflict_response = decision.response
                    conflict_provenance = decision_provenance
                    conflict_silent = decision.finish_silently
                    continue
                memory_conflicts = ()
                if not committed:
                    return CoreOutcome(CoreState.ERROR, snapshot, reason="memory_persistence_error")
                # A plan that runs on is applied now, before anything below
                # can park the goal. A finish or cancel is applied only where
                # this decision actually ends, in one write with her reply.
                closing = (decision.plan_update is not None and decision.plan_update.operation
                           in (PlanOperation.FINISH, PlanOperation.CANCEL))
                if decision.plan_update is not None and not closing:
                    assert snapshot is not None
                    snapshot, _ = self._apply_plan_update(
                        snapshot, decision.plan_update, conversation, decision_provenance,
                    )
                if mechanical_blocker is not None:
                    reply_turn = None
                    if closing:
                        snapshot, reply_turn = self._apply_plan_update(
                            snapshot, decision.plan_update, conversation,
                            decision_provenance, decision.response,
                        )
                    if snapshot is not None:
                        snapshot = self._park_unfinished_goal(snapshot, decision_provenance)
                    return CoreOutcome(
                        CoreState.RESPONDED if decision.response is not None else CoreState.CHECKPOINTED,
                        snapshot, response=decision.response, reason=mechanical_blocker,
                        response_provenance=decision_provenance,
                        response_turn_id=reply_turn,
                    )
                if decision.selects_only:
                    if (proposal_error is None and decision.goal_proposal is not None
                            and previous is not None
                            and snapshot is not None and snapshot.state == previous.state):
                        return CoreOutcome(
                            CoreState.CHECKPOINTED, snapshot, reason="goal_selection_no_progress",
                        )
                    # Selection with proposals is still an intermediate step.
                    # Its writes have used the canonical persistence path above;
                    # continue within this turn's existing budget and attempts.
                    continue
                # A rejected optional mutation must not let old outstanding
                # work suppress an independent answer. Dependent responses and
                # silence have already failed above; selection-only decisions
                # have continued, and memory checks still run before this point.
                if deferred_selection is not None:
                    if snapshot is not None:
                        snapshot = self._park_unfinished_goal(snapshot, decision_provenance)
                elif proposal_error is None:
                    snapshot, deferred = self._defer_or_park_premature_end(
                        snapshot,
                        approved_dispatches,
                        continuation_notice_issued,
                        step_index,
                        step_budget,
                        decision_provenance,
                    )
                    if deferred is not None:
                        continuation_notice_issued = True
                        continuation_notices = deferred
                        continue
                reply_turn = None
                if closing:
                    snapshot, reply_turn = self._apply_plan_update(
                        snapshot, decision.plan_update, conversation,
                        decision_provenance, decision.response,
                    )
                if decision.finish_silently:
                    return CoreOutcome(
                        CoreState.FINISHED_SILENTLY,
                        snapshot,
                        reason="core_selected_silence",
                    )
                return CoreOutcome(
                    CoreState.RESPONDED,
                    snapshot,
                    response=decision.response,
                    reason="goal_proposal_rejected" if proposal_error else deferred_selection,
                    response_provenance=decision_provenance,
                    response_turn_id=reply_turn,
                )

            if snapshot is None or snapshot.state.status is not GoalStatus.ACTIVE:
                # Only a side-effect-free call reaches here; an effectful one
                # without an active goal was stopped above.
                if any(item.call is not None and item.call.call_id == decision.call.call_id
                       for item in transient_attempts):
                    if self._already_refused(
                        refused_calls, "call_id_reused", decision.call.call_id
                    ):
                        return CoreOutcome(
                            CoreState.ERROR, snapshot, reason="call_id_reused"
                        )
                    LOGGER.info("Call rejected: call_id_reused")
                    refused_calls = (*refused_calls, {
                        "call_id": decision.call.call_id,
                        "capability_id": decision.call.capability_id,
                        "reason": "call_id_reused",
                        "subject": decision.call.call_id,
                    })
                    continue
                try:
                    snapshot, committed = self._commit_memories(
                        snapshot, decision.memory_proposals, retention_until)
                except MemoryIdentityConflict:
                    memory_conflicts = self._conflicting_memories(
                        decision.memory_proposals, retention_until)
                    continue
                memory_conflicts = ()
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
                continuation_notice_issued = False
                continue
            coding_exhaustion = (
                (self._coding_exhaustion_reason(snapshot.state, decision.call)
                 or self._coding_interruption_exhaustion_reason(snapshot.state, decision.call))
                if decision.call.capability_id == _RUN_CODING_TASK
                else None
            )
            if coding_exhaustion is not None:
                # A bounded Coding Agent path is already closed. Treat even a
                # repeated call identifier as the same capability refusal so
                # it cannot escalate into a Core-level call-id failure.
                already_exhausted = self._coding_retry_already_exhausted(
                    snapshot.state, coding_exhaustion
                )
                if not already_exhausted:
                    refusal = CapabilityAttempt(
                        decision.call,
                        CapabilityAttemptDisposition.REJECTED,
                        False,
                        reason_code=coding_exhaustion,
                    )
                    snapshot = self._store.replace(
                        replace(
                            snapshot.state,
                            attempts=(*snapshot.state.attempts, refusal),
                        ),
                        snapshot.retention_until,
                        snapshot.revision,
                        decision_provenance,
                    )
                refused_calls = (*refused_calls, {
                    "call_id": decision.call.call_id,
                    "capability_id": decision.call.capability_id,
                    "reason": coding_exhaustion,
                    "subject": coding_exhaustion,
                })
                if already_exhausted and origin is CognitionOrigin.PERSON_TURN:
                    return self._respond_to_terminal_blocker(
                        conversation_id, conversation, snapshot, reasoning_context,
                        transient_attempts, coding_exhaustion, decision_provenance,
                        refused_calls,
                    )
                if already_exhausted:
                    mechanical_blocker = coding_exhaustion
                continue
            if self._call_id_exists(snapshot.state, decision.call.call_id):
                if self._already_refused(
                    refused_calls, "call_id_reused", decision.call.call_id
                ):
                    return CoreOutcome(
                        CoreState.ERROR, snapshot, reason="call_id_reused"
                    )
                LOGGER.info("Call rejected: call_id_reused")
                refused_calls = (*refused_calls, {
                    "call_id": decision.call.call_id,
                    "capability_id": decision.call.capability_id,
                    "reason": "call_id_reused",
                    "subject": decision.call.call_id,
                })
                continue
            if (
                decision.call.capability_id in self._turn_bound_capabilities
                and decision.call.capability_id in approved_dispatches
            ):
                # The same invariant as the approval check above, enforced on
                # the other route to a dispatch. A call may carry an approval
                # granted in an earlier step instead of proposing a new one,
                # and that path never reaches the proposal check at all.
                LOGGER.info(
                    "Second approved dispatch refused this turn: %s",
                    decision.call.capability_id,
                )
                refusal = CapabilityAttempt(
                    decision.call,
                    CapabilityAttemptDisposition.REJECTED,
                    False,
                    reason_code="approval_capability_already_dispatched",
                )
                snapshot = self._store.replace(
                    replace(
                        snapshot.state,
                        attempts=(*snapshot.state.attempts, refusal),
                    ),
                    snapshot.retention_until,
                    snapshot.revision,
                    decision_provenance,
                )
                return CoreOutcome(
                    CoreState.CHECKPOINTED,
                    snapshot,
                    reason="approval_capability_already_dispatched",
                )
            refusal = self._binding_rejected_call(snapshot.state, decision.call, now)
            if refusal is not None:
                refused_calls = (*refused_calls, {
                    "call_id": decision.call.call_id,
                    "capability_id": decision.call.capability_id,
                    "reason": "repeated_rejected_call",
                    "subject": refusal.reason_code,
                })
                if origin is CognitionOrigin.PERSON_TURN:
                    return self._respond_to_terminal_blocker(
                        conversation_id, conversation, snapshot, reasoning_context,
                        transient_attempts, "repeated_rejected_call",
                        decision_provenance, refused_calls,
                    )
                return CoreOutcome(
                    CoreState.CHECKPOINTED, snapshot,
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
                                           snapshot.revision, decision_provenance)
            try:
                snapshot, committed = self._commit_memories(
                    snapshot, decision.memory_proposals, retention_until)
            except MemoryIdentityConflict:
                memory_conflicts = self._conflicting_memories(
                    decision.memory_proposals, retention_until)
                continue
            memory_conflicts = ()
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
            if (
                decision.call.capability_id in self._turn_bound_capabilities
                and attempt.implementation_invoked
            ):
                # The instruction is spent only once an implementation may
                # have acted. Broker and safety rejections happen before that
                # boundary, so a corrected call may still use the same turn.
                approved_dispatches.add(decision.call.capability_id)
            snapshot = self._finalize_dispatch(snapshot, attempt, now)
            if (decision.call.capability_id in {
                "request_external_review", "read_external_review", "merge_pull_request"
            } and attempt.result is not None
                    and (attempt.result.failure or {}).get("requires_judgement")):
                mechanical_blocker = str(attempt.result.failure["code"])
                if origin is CognitionOrigin.PERSON_TURN:
                    return self._respond_to_terminal_blocker(
                        conversation_id, conversation, snapshot, reasoning_context,
                        transient_attempts, mechanical_blocker, decision_provenance,
                    )
            continuation_notice_issued = False
        if (
            snapshot is not None
            and snapshot.state.status is GoalStatus.ACTIVE
            and (snapshot.state.outstanding_work or snapshot.state.blockers)
        ):
            # Remaining work will not run without another step. Park rather
            # than leave ACTIVE checkpointed work that looks as if it is
            # still going to execute.
            try:
                now = self._clock()
                if now.tzinfo is None or now.utcoffset() is None:
                    raise ValueError("Core clock must be timezone-aware")
                snapshot = self._park_unfinished_goal(
                    snapshot,
                    self._derived_provenance(
                        now, conversation, snapshot, retrieved_memories,
                        transient_attempts, prior_goal_provenance,
                    ),
                )
            except Exception:
                LOGGER.info("Unfinished goal could not be parked after step budget")
        if conflict_response is not None or conflict_silent:
            # She finished what she wanted to say, then ran out of steps while
            # resolving a memory identifier. Discarding the answer to report a
            # memory problem would lose the part that mattered to the person,
            # so it is delivered and the memory failure is stated separately.
            return CoreOutcome(
                CoreState.FINISHED_SILENTLY if conflict_silent else CoreState.RESPONDED,
                snapshot,
                response=None if conflict_silent else conflict_response,
                reason="core_selected_silence" if conflict_silent else None,
                response_provenance=None if conflict_silent else conflict_provenance,
                memory_state="unresolved_identity_conflict",
            )
        return CoreOutcome(CoreState.CHECKPOINTED, snapshot, reason="budget_exhausted")

    def _respond_to_terminal_blocker(
        self,
        conversation_id: str,
        conversation: ConversationSnapshot,
        snapshot: GoalSnapshot | None,
        context: ReasoningContext,
        transient_attempts: tuple[CapabilityAttempt, ...],
        reason: str,
        provenance: ContentProvenance,
        refused_calls: tuple[Mapping[str, Any], ...] = (),
        park: bool = True,
    ) -> CoreOutcome:
        """Allow one response-only decision for a person, parking stopped work.

        A refused plan change stops nothing that was running, so it parks
        nothing: she only says what happened. There may be no goal at all,
        when what was refused was the decision that would have created one.
        """
        if park and snapshot is not None:
            snapshot = self._park_unfinished_goal(snapshot, provenance)
        state = None if snapshot is None else snapshot.state
        terminal_context = replace(
            context,
            active_goal=state,
            turns=project_turns_for_reasoning(conversation.turns, state),
            unfinished_goals=self._selectable_goals(conversation_id, snapshot),
            transient_attempts=transient_attempts,
            response_only_reason=reason,
            refused_calls=refused_calls or context.refused_calls,
        )
        try:
            self._budget_check(conversation_id)
        except Exception as error:
            LOGGER.warning("Terminal checkpoint response stopped by execution budget: %s", error)
            return CoreOutcome(CoreState.CHECKPOINTED, snapshot, reason=reason)
        try:
            decision = self._reasoner.decide(terminal_context)
        except Exception as error:
            LOGGER.info("Reasoner decision rejected: %s: %s", type(error).__name__, error)
            return CoreOutcome(CoreState.ERROR, snapshot, reason="reasoner_error")
        # A test double or a nonconforming provider can bypass the structured
        # schema. The already selected goal ID is only referential metadata;
        # any different ID would navigate to another goal. Reject that and all
        # work before any mutation, memory write or call.
        if (
            decision.response is None
            or decision.call is not None
            or decision.memory_query is not None
            or decision.goal_id not in (None, None if state is None else state.goal_id)
            or decision.goal_proposal is not None
            or decision.memory_proposals
            or decision.approval_proposal is not None
            or decision.response_requires_goal_commit
            or decision.finish_silently
        ):
            LOGGER.info("Terminal checkpoint response rejected: not response only")
            return CoreOutcome(CoreState.CHECKPOINTED, snapshot, reason=reason)
        return CoreOutcome(
            CoreState.RESPONDED, snapshot, response=decision.response,
            reason=reason, response_provenance=provenance,
        )

    def _close_interrupted_dispatch(self, snapshot: GoalSnapshot) -> GoalSnapshot:
        """Resolve an interrupted dispatch as an unknown outcome, once."""
        pending = tuple(item for item in snapshot.state.attempts
                        if item.disposition is CapabilityAttemptDisposition.PENDING)
        if len(pending) != 1 or pending[0].call is None:
            return snapshot
        call = pending[0].call
        if call.call_id in self._live_plan_dispatches:
            # A planned step a background worker is still running. It has
            # not been interrupted; its worker records the result.
            return snapshot
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
        return self._finalize_dispatch(snapshot, closed, self._clock())

    def reconcile_dispatch(self, goal_id: str, attempt: CapabilityAttempt) -> CoreOutcome:
        snapshot = self._store.load(goal_id)
        pending = tuple(item for item in snapshot.state.attempts
                        if item.disposition is CapabilityAttemptDisposition.PENDING)
        if len(pending) != 1:
            return CoreOutcome(CoreState.ERROR, snapshot, reason="pending_dispatch_missing")
        if attempt.disposition is CapabilityAttemptDisposition.PENDING or attempt.call != pending[0].call:
            return CoreOutcome(CoreState.ERROR, snapshot, reason="attempt_invalid")
        snapshot = self._finalize_dispatch(snapshot, attempt, self._clock())
        return CoreOutcome(CoreState.CHECKPOINTED, snapshot, reason="dispatch_reconciled")

    @staticmethod
    def _objective_source(
        conversation: ConversationSnapshot, trigger_event_id: str | None
    ) -> str:
        """Name what this goal actually arose from.

        A turn begun by an arriving event names that event. Preferring the
        latest person turn regardless attributed an event-driven goal to
        whatever was last said, which in an established conversation is
        unrelated to the message that triggered it.
        """
        if trigger_event_id is not None:
            return f"event:{trigger_event_id}"
        for item in reversed(conversation.turns):
            if item.person_id is not None:
                return f"turn:{item.turn_id}"
        if conversation.events:
            return f"event:{conversation.events[-1].event_id}"
        if conversation.turns:
            return f"turn:{conversation.turns[-1].turn_id}"
        return f"conversation:{conversation.conversation_id}"

    def _reduce_goal_proposal(self, snapshot: GoalSnapshot | None,
                              proposal: GoalProposal | None,
                              conversation: ConversationSnapshot,
                              trigger_event_id: str | None = None) -> tuple[GoalState | None, str | None]:
        if proposal is None:
            return None if snapshot is None else snapshot.state, None
        if snapshot is None:
            if proposal.kind is not GoalMutationKind.CREATE:
                return None, "goal_missing"
            return self._create_goal(proposal, conversation, trigger_event_id)

        if proposal.kind is GoalMutationKind.CREATE:
            # A conversation may hold several independent unfinished goals.
            # Creating one never depends on the state of another: the goal
            # the Core was working under, if any, is left exactly as it is.
            return self._create_goal(proposal, conversation, trigger_event_id)
        state = snapshot.state
        if state.status in (GoalStatus.COMPLETED, GoalStatus.CANCELLED):
            return state, "goal_inactive"
        proposal, replay_error = self._without_replayed_evidence(state, proposal)
        if replay_error:
            return state, replay_error
        evidence_only = replace(
            state, evidence=(*state.evidence, *proposal.new_evidence),
        )
        error = self._evidence_grounding_error(
            conversation, evidence_only, state.evidence, proposal.new_evidence,
        )
        if error:
            return state, error
        history_error = self._history_proposal_error(state, proposal)
        if history_error:
            return evidence_only, history_error
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
            updated = self._derive_goal_status(updated, proposal.kind)
        except (TypeError, ValueError) as error_value:
            # The evidence was grounded above.  A mutation-specific failure
            # must not erase it, but none of the requested state mutation is
            # allowed to survive this return.
            return evidence_only, str(error_value)
        return updated, None

    @staticmethod
    def _without_replayed_evidence(
        state: GoalState, proposal: GoalProposal,
    ) -> tuple[GoalProposal, str | None]:
        """Make an identical retry of already durable evidence idempotent.

        A correction may repeat the evidence that was accepted while its prior
        mutation was refused.  It is not a second durable record.  Reusing an
        identifier for changed content remains a rejected history collision.
        """
        existing = {item.evidence_id: item for item in state.evidence}
        retained = []
        for item in proposal.new_evidence:
            prior = existing.get(item.evidence_id)
            if prior is None:
                retained.append(item)
            elif prior != item:
                return proposal, "durable_record_id_reused"
        return replace(proposal, new_evidence=tuple(retained)), None

    def _create_goal(self, proposal: GoalProposal,
                     conversation: ConversationSnapshot,
                     trigger_event_id: str | None) -> tuple[GoalState | None, str | None]:
        if proposal.objective_summary is None or not proposal.success_criteria:
            return None, "goal_creation_incomplete"
        creation_error = self._new_history_error(proposal)
        if creation_error:
            return None, creation_error
        state = GoalState(
            self._identifier_factory(),
            Objective(
                # A goal begun by an arriving event has no person turn to
                # attribute it to, and attributing it to an unrelated
                # earlier turn would misstate where it came from.
                self._objective_source(conversation, trigger_event_id),
                proposal.objective_summary,
            ),
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

    def _persist_goal(self, candidate: GoalState, previous: GoalSnapshot | None,
                      conversation: ConversationSnapshot, retention_until: datetime,
                      provenance: ContentProvenance) -> GoalSnapshot:
        """Write the reduced goal: a new record, or a revision of the attached one."""
        if previous is None or candidate.goal_id != previous.state.goal_id:
            return self._store.create(
                candidate, conversation.conversation_id, retention_until, provenance,
            )
        return self._store.replace(
            candidate, previous.retention_until, previous.revision, provenance,
        )

    def _selectable_goals(self, conversation_id: str,
                          snapshot: GoalSnapshot | None) -> tuple[GoalSummary, ...]:
        """The unfinished goals, plus the selected one whatever its status.

        A goal the Core has just completed or cancelled leaves the unfinished
        list, but it is still the goal this turn is working under and its
        state is still what the Core is reasoning about. Dropping it here
        would contradict the reasoning context on the very next step.
        """
        # The project the current work belongs to, when there is current work.
        # A storage fact read off the selected goal, never inferred from what
        # was said.
        active_project = (
            None
            if snapshot is None or snapshot.scope is None
            else snapshot.scope.project_id
        )
        ordinary = self._store.list_unfinished(
            conversation_id,
            project_id=active_project,
            limit=UNFINISHED_GOAL_CANDIDATES,
        )
        summaries = self._with_attention(ordinary)
        if snapshot is None:
            return summaries
        if any(item.goal_id == snapshot.state.goal_id for item in summaries):
            return summaries
        # The goal being worked under is always visible, even when the cap
        # would have excluded it. Dropping it would contradict the reasoning
        # context on the very next step, so it replaces the lowest-priority
        # candidate rather than extending the bound.
        selected = GoalSummary.of(
            snapshot.state,
            project_id=active_project,
            from_current_conversation=(
                snapshot.conversation_id == conversation_id
            ),
        )
        kept = ordinary[: UNFINISHED_GOAL_CANDIDATES - 1]
        return self._with_attention((*kept, selected))

    def _with_attention(self, ordinary: tuple[GoalSummary, ...]) -> tuple[GoalSummary, ...]:
        """The ordinary candidates, then plans that need her, separately bounded."""
        return (*ordinary, *self._store.list_needing_core(
            PLAN_ATTENTION_CANDIDATES,
            exclude=frozenset(item.goal_id for item in ordinary),
        ))

    @staticmethod
    def _goal_selection_error(decision: AgentDecision, snapshot: GoalSnapshot | None,
                              summaries: tuple[GoalSummary, ...],
                              selected_goals: set[str]) -> str | None:
        """Permit each offered goal's full-state inspection once per turn."""
        assert decision.goal_id is not None
        current = None if snapshot is None else snapshot.state
        already_selected = current is not None and current.goal_id == decision.goal_id
        # Detect revisits even when completion removed the goal from summaries.
        if not already_selected and decision.goal_id in selected_goals:
            return "goal_selection_revisited"
        if not any(item.goal_id == decision.goal_id for item in summaries):
            return "goal_selection_unknown"
        if already_selected:
            if decision.selects_only and decision.goal_proposal is None:
                return "goal_selection_redundant"
            return None
        return None

    def _definition(self, capability_id: str) -> CapabilityDefinition | None:
        return next(
            (item for item in self._capabilities if item.capability_id == capability_id),
            None,
        )

    def _immediately_executable_remaining_work(
        self,
        state: GoalState | None,
        approved_dispatches: set[str],
    ) -> bool:
        """Whether the selected goal still has work this turn can run.

        Taken only from durable goal fields and turn-local spend: ACTIVE,
        nonempty outstanding_work, no blockers, and remaining actions not
        already spent by a turn-bound capability dispatched this turn.
        """
        if state is None or state.status is not GoalStatus.ACTIVE:
            return False
        if not state.outstanding_work or state.blockers:
            return False
        if approved_dispatches & self._turn_bound_capabilities:
            return False
        # Remaining work a plan is executing is not this turn's to run.
        return not self._plan_is_executing(state)

    def _park_unfinished_goal(
        self,
        snapshot: GoalSnapshot,
        provenance: ContentProvenance,
    ) -> GoalSnapshot:
        """Reduce an ACTIVE goal that cannot continue to a truthful parked status.

        Does not complete the goal: completion still requires REQUEST_COMPLETION
        and sourced evidence.
        """
        state = snapshot.state
        if state.status is not GoalStatus.ACTIVE:
            return snapshot
        if self._plan_is_executing(state):
            # Work the plan executor is carrying out is going to execute. It
            # is not parked because a turn ended; only she stops it.
            return snapshot
        if state.blockers:
            kind = GoalMutationKind.BLOCK
        elif state.outstanding_work:
            kind = GoalMutationKind.AWAIT_INPUT
        else:
            return snapshot
        return self._store.replace(
            self._derive_goal_status(state, kind),
            snapshot.retention_until,
            snapshot.revision,
            provenance,
        )

    def _defer_or_park_premature_end(
        self,
        snapshot: GoalSnapshot | None,
        approved_dispatches: set[str],
        continuation_notice_issued: bool,
        step_index: int,
        step_budget: int,
        provenance: ContentProvenance,
    ) -> tuple[GoalSnapshot | None, tuple[Mapping[str, Any], ...] | None]:
        """Continue the step loop, park, or let a call-less decision stand.

        A deferred end returns a one-shot continuation notice and does not
        deliver the premature response. Any other remaining unfinished work
        is parked before the person-facing outcome is returned.
        """
        state = None if snapshot is None else snapshot.state
        if self._immediately_executable_remaining_work(state, approved_dispatches):
            assert state is not None
            if not continuation_notice_issued and step_index + 1 < step_budget:
                LOGGER.info(
                    "Premature turn end deferred: remaining work still executable"
                )
                return snapshot, ({
                    "reason": "remaining_work_still_executable",
                    "outstanding_work": [
                        item.item_id for item in state.outstanding_work
                    ],
                },)
            if snapshot is not None:
                snapshot = self._park_unfinished_goal(snapshot, provenance)
            return snapshot, None
        if (
            snapshot is not None
            and snapshot.state.status is GoalStatus.ACTIVE
            and (snapshot.state.outstanding_work or snapshot.state.blockers)
        ):
            snapshot = self._park_unfinished_goal(snapshot, provenance)
        return snapshot, None

    def _dispatch_blocked_reason(self, call: CapabilityCall,
                                 state: GoalState | None) -> str | None:
        """Why a call cannot be dispatched in this goal state, if it cannot.

        A side-effect-free call may serve plain conversation. Anything else
        needs a goal that is active right now: a paused, finished or absent
        goal cannot carry an approval or an attempt, so the dispatch is
        impossible rather than merely refused, and reasoning again from the
        same state cannot make it possible.
        """
        if state is not None and self._has_pending_dispatch(state):
            # A planned step is running on this goal in the background. One
            # dispatch at a time per goal; the plan's own result decides
            # what follows it.
            return "plan_step_in_flight"
        definition = self._definition(call.capability_id)
        if definition is not None and definition.side_effect in (
            SideEffect.NONE,
            SideEffect.ATTENTION_STATE,
        ):
            return None
        if state is None or state.status is not GoalStatus.ACTIVE:
            return "active_goal_required"
        return None

    # Text AL/X composed herself and is about to send outward must already have
    # reached Friedl in something she said. This is a property of the artifact,
    # not an ordering rule: she may draft, ask, argue, or change her mind in any
    # order, and needs no permission to speak. She simply cannot transmit words
    # he never heard, which is what stops her reporting one message and sending
    # another.
    # Every argument whose value AL/X composes rather than copies from a
    # message. A recipient or subject he never heard is as much a surprise as
    # a body he never heard.
    _AUTHORED_TEXT_ARGUMENTS = frozenset({"body", "body_text", "subject"})

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
        # Only what AL/X said most recently counts. Scanning every turn she has
        # ever spoken let a draft from earlier in the conversation satisfy the
        # rule, so an answer to a later, unrelated question authorised a send
        # she had not just proposed. The wording must be in the message she
        # just gave him, immediately before he answered.
        latest = next(
            (
                item for item in reversed(conversation.turns)
                if item.origin is ConversationOrigin.ALX_RESPONSE
            ),
            None,
        )
        spoken = "" if latest is None else " ".join(latest.content.split())
        return any(
            " ".join(item.split()) not in spoken for item in authored
        )

    def _approval_proposal_error(
        self, conversation, state, decision, already_dispatched=frozenset()
    ) -> str | None:
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
        if (
            call.capability_id in self._turn_bound_capabilities
            and call.capability_id in already_dispatched
        ):
            # Already done once on this instruction. A second needs a second
            # instruction, which is what Friedl is asked for.
            return "approval_capability_already_dispatched"
        # Only a capability that actually carries her wording to someone else
        # is held to what Friedl has already heard. Scoped by the capability's
        # own declaration rather than by argument names: a search argument
        # called `subject` once made every web search look like unsent mail,
        # and sixteen refusals in one turn told her nothing about why.
        definition = self._definition(call.capability_id)
        if definition is not None and definition.transmits_authored_text:
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
        evidence_ids = history_evidence_ids(state.evidence, proposal.new_evidence)
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
        evidence_ids.update(history_evidence_ids(proposal.new_evidence))
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
            succeeded_attempts = {
                f"attempt:{item.call.call_id}"
                for item in state.attempts
                if CoreAgent._attempt_is_citable_evidence_source(item)
                and item.result.state is CapabilityResultState.SUCCEEDED
            }
            supported: set[str] = set()
            for item in state.evidence:
                if not item.source_references:
                    continue
                if any(
                    reference.startswith("attempt:")
                    and reference not in succeeded_attempts
                    for reference in item.source_references
                ):
                    # A failed attempt remains durable historical evidence,
                    # but cannot prove any completion criterion.
                    continue
                supported.update(item.supports)
            required = {item.criterion_id for item in state.success_criteria}
            if not required.issubset(supported):
                raise ValueError("completion_lacks_sourced_evidence")
            return replace(state, status=GoalStatus.COMPLETED,
                           stop_reason=GoalStopReason.SUCCESS_CRITERIA_MET)
        if kind is GoalMutationKind.AWAIT_INPUT:
            # Awaiting input means work is genuinely parked on an answer, so
            # the record has to name that work. Where there is none, the goal
            # is not waiting on Friedl; it is simply active with nothing
            # pending, and the response itself hands the turn back.
            #
            # Staying ACTIVE is the honest reading of that state, and it is the
            # only one available: inventing an outstanding item would fabricate
            # work nobody asked for, and completing the goal would claim a
            # result no evidence supports. Failing the turn instead is what
            # this replaces - a stale goal in exactly this shape refused every
            # further turn on its conversation, so no new instruction could be
            # acted on at all.
            if not state.outstanding_work:
                return replace(state, status=GoalStatus.ACTIVE, stop_reason=None)
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
    def _attempt_is_citable_evidence_source(item: CapabilityAttempt) -> bool:
        """Whether this attempt may be named as attempt:<call-id> evidence.

        A terminal attempt with a result is a real source, including FAILED
        jobs and PARTIAL invoice reads that ran. Partial evidence is not
        posting success. PENDING and never-executed attempts are not. Citing
        a failed result records that it happened; it does not prove success.
        """
        if item.call is None:
            return False
        if item.disposition is CapabilityAttemptDisposition.PENDING:
            return False
        if item.implementation_invoked is not True:
            return False
        result = item.result
        if result is None:
            return False
        return result.state in {
            CapabilityResultState.SUCCEEDED,
            CapabilityResultState.FAILED,
            CapabilityResultState.PARTIAL,
        }

    @staticmethod
    def _evidence_grounding_error(conversation: ConversationSnapshot,
                                  state: GoalState, existing: tuple,
                                  proposed: tuple) -> str | None:
        known = {f"turn:{item.turn_id}" for item in conversation.turns}
        known.update(f"event:{item.event_id}" for item in conversation.events)
        known.update(
            f"attempt:{item.call.call_id}"
            for item in state.attempts
            if CoreAgent._attempt_is_citable_evidence_source(item)
        )
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

    def _finalize_dispatch(
        self,
        snapshot: GoalSnapshot,
        attempt: CapabilityAttempt,
        recorded_at: datetime,
    ) -> GoalSnapshot:
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
        attempts = snapshot.state.attempts
        # The pending attempt this result closes, found by its call. A plan's
        # background step is the latest attempt too, but finding it by
        # identity is what makes that a fact rather than an assumption.
        index = next(
            (position for position in range(len(attempts) - 1, -1, -1)
             if attempts[position].call is not None
             and attempts[position].call.call_id == attempt.call.call_id
             and attempts[position].disposition is CapabilityAttemptDisposition.PENDING),
            len(attempts) - 1,
        )
        updated = replace(snapshot.state,
                          attempts=(*attempts[:index], attempt, *attempts[index + 1:]),
                          approvals=approvals)
        inputs = tuple(
            item
            for item in (
                snapshot.provenance,
                None if attempt.result is None else attempt.result.provenance,
            )
            if item is not None
        )
        provenance = (
            None
            if not inputs
            else RetentionPolicy().derive(ContentOrigin.ALX, recorded_at, inputs)
        )
        return self._store.replace(
            updated,
            snapshot.retention_until,
            snapshot.revision,
            provenance,
        )

    # ------------------------------------------------------------------
    # D-036 execution plans. The runner below advances work AL/X decided;
    # every result passes `classify_planned_result` and `reduce_plan` once.
    # She changes a plan only through `_apply_plan_update`.
    # ------------------------------------------------------------------

    def advance_due_plans(self) -> tuple[PlannedDispatch, ...]:
        """Reconcile every open plan and checkpoint the steps now due.

        Runs under the Core-turn lock, so it only reads and writes durable
        state: a recorded result is reduced, an interrupted dispatch is
        closed, a finished goal's plan is closed, each step that is due is
        checkpointed, and evidence an attention lost to a restart is
        scheduled to be read again. Each is returned for a background worker.
        This is also restart recovery; there is no other.
        """
        jobs = []
        for goal_id in self._store.list_open_plan_goal_ids():
            try:
                job = self._advance_plan(self._store.load(goal_id))
            except Exception as error:  # noqa: BLE001 - one plan must not stop the rest
                # Its durable state is whatever it last reached, so the next
                # tick tries again from there.
                LOGGER.warning("Plan reconciliation failed for goal %s: %s",
                               goal_id, type(error).__name__)
                continue
            if job is not None:
                jobs.append(job)
        return tuple(jobs)

    def begin_planned_dispatch(self, job: PlannedDispatch) -> PlannedDispatch | None:
        """Cross the dispatch boundary, or decline to. Under the lock.

        Immediately before a worker calls the capability, the plan must still
        hold this exact checkpoint: the same plan, step and dispatch, still
        executable, its declared preconditions and approval still valid. Then
        `started` is written durably and the job is returned, with the
        authority the safety gate will evaluate. Otherwise nothing is called:
        the checkpoint is closed as never invoked, its approval is returned,
        and only a plan that is still current is told why. This is the
        linearization point for cancellation: a cancel or replacement that
        commits before it prevents the call; one after it can stop only
        what follows.
        """
        snapshot = self._store.load(job.goal_id)
        state = snapshot.state
        if not any(item.call is not None and item.call.call_id == job.call.call_id
                   and item.disposition is CapabilityAttemptDisposition.PENDING
                   for item in state.attempts):
            self._live_plan_dispatches.discard(job.call.call_id)
            return None
        plan = state.execution_plan
        if job.evidence_for is not None:
            if (plan is not None and plan.plan_id == job.plan_id
                    and plan.status is PlanStatus.NEEDS_CORE
                    and plan.attention_seq == job.attention_seq
                    and state.status is GoalStatus.ACTIVE):
                return job
            self._withdraw_dispatch(snapshot, job, "plan_evidence_withdrawn")
            return None
        if (plan is not None and plan.inflight is not None
                and plan.inflight.call_id == job.call.call_id and plan.inflight.started):
            # Already across the boundary: the call may be in flight, so its
            # record is left exactly as it is. Never begun twice.
            return None
        if (plan is None or plan.plan_id != job.plan_id
                or plan.status is not PlanStatus.RUNNING or plan.inflight is None
                or plan.inflight.call_id != job.call.call_id):
            # Cancelled or replaced before the call: it never happens.
            self._withdraw_dispatch(snapshot, job, "plan_dispatch_withdrawn")
            return None
        now = self._clock()
        # The approval was claimed at the checkpoint. The gate sees it as the
        # grant it still is, unless it has since been withdrawn or expired.
        authority = replace(state, approvals=tuple(
            replace(item, lifecycle=ApprovalLifecycle.GRANTED)
            if (item.lifecycle is ApprovalLifecycle.CLAIMED
                and item.approval_id == job.call.approval_id
                and item.scope.matches(job.call)) else item
            for item in state.approvals
        ))
        facts = plan_invalidation_facts(plan, authority, now)
        if facts:
            snapshot = self._withdraw_dispatch(snapshot, job, "plan_dispatch_withdrawn")
            self._write_plan(snapshot, raise_attention(
                snapshot.state.execution_plan, facts, now))
            return None
        self._write_plan(snapshot, replace(
            plan, inflight=replace(plan.inflight, started=True)))
        return replace(job, authority=authority)

    def _withdraw_dispatch(self, snapshot: GoalSnapshot, job: PlannedDispatch,
                           reason: str) -> GoalSnapshot:
        """Close a checkpoint whose call was never made, and return its approval."""
        self._live_plan_dispatches.discard(job.call.call_id)
        LOGGER.info("Planned dispatch for goal %s withdrawn: %s", job.goal_id, reason)
        return self._finalize_dispatch(snapshot, self._never_called(job.call, reason),
                                       self._clock())

    def run_planned_dispatch(self, job: PlannedDispatch) -> CapabilityAttempt | None:
        """Dispatch one started step. Runs off the lock, on a worker.

        Touches no store: the checkpoint is already durable and the result is
        recorded by `finish_planned_dispatch`. None means the dispatch did
        not return a usable attempt, which is recorded as an interruption.
        """
        self._bind_dispatch(job.conversation_id)
        try:
            attempt = self._dispatch(job.call, job.authority)
        except Exception as error:  # noqa: BLE001 - recorded, never replayed
            LOGGER.warning("Planned dispatch raised for %s: %s",
                           job.call.capability_id, type(error).__name__)
            return None
        if attempt.call != job.call or attempt.disposition is CapabilityAttemptDisposition.PENDING:
            return None
        return attempt

    def finish_planned_dispatch(
        self, job: PlannedDispatch, attempt: CapabilityAttempt | None,
        *, continue_plan: bool = True,
    ) -> tuple[PlannedDispatch, ...]:
        """Record a background step's result and reduce it. Under the lock.

        The result is always recorded on the goal. It moves the plan only if
        the plan still holds this exact dispatch in flight: a plan she
        cancelled or replaced meanwhile is untouched. An evidence read is
        kept only for the attention it was read for. When the plan can run
        on, and the runtime is not stopping, its next step is checkpointed
        and returned at once.
        """
        self._live_plan_dispatches.discard(job.call.call_id)
        snapshot = self._store.load(job.goal_id)
        if attempt is None:
            attempt = self._interrupted(job.call)
        if any(item.call is not None and item.call.call_id == job.call.call_id
               and item.disposition is CapabilityAttemptDisposition.PENDING
               for item in snapshot.state.attempts):
            snapshot = self._finalize_dispatch(snapshot, attempt, self._clock())
        plan = snapshot.state.execution_plan
        if job.evidence_for is not None:
            self._evidence_reads.pop(job.call.call_id, None)
            self._evidence_settled.add((job.goal_id, job.attention_seq, job.evidence_for))
            if (plan is not None and plan.plan_id == job.plan_id
                    and plan.attention_seq == job.attention_seq
                    and plan.status is PlanStatus.NEEDS_CORE
                    and attempt.disposition is CapabilityAttemptDisposition.EXECUTED):
                self._plan_evidence_cache[job.evidence_for] = attempt
            return ()
        if (plan is None or plan.plan_id != job.plan_id or plan.inflight is None
                or plan.inflight.call_id != job.call.call_id):
            LOGGER.info("Planned result for %s no longer belongs to a plan", job.goal_id)
            return ()
        snapshot = self._reduce_planned_result(snapshot)
        if not continue_plan:
            return ()
        next_job = self._advance_plan(snapshot)
        return () if next_job is None else (next_job,)

    def plan_evidence_ready(self, goal_id: str) -> bool:
        """Whether a plan's attention can be shown to her with its evidence.

        False only while an evidence read this process scheduled, or must
        still schedule, has not returned. Offering the attention then would
        buy a reasoning call over metadata alone.
        """
        snapshot = self._store.load(goal_id)
        plan = snapshot.state.execution_plan
        if plan is None or plan.attention is None or snapshot.state.status is not GoalStatus.ACTIVE:
            return True
        return all(
            call_id in self._plan_evidence_cache
            or not self._needs_reread(snapshot.state, call_id)
            or (goal_id, plan.attention_seq, call_id) in self._evidence_settled
            for call_id in plan.attention.evidence_call_ids
        )

    def _needs_reread(self, state: GoalState, call_id: str) -> bool:
        stored = next((item for item in state.attempts
                       if item.call is not None and item.call.call_id == call_id), None)
        definition = None if stored is None else self._definition(stored.call.capability_id)
        return definition is not None and definition.plan_observation

    def _evidence_job(self, snapshot: GoalSnapshot) -> PlannedDispatch | None:
        """Schedule the read of evidence a restart left as metadata only.

        Only a declared observation is read again, on a background worker
        like any planned step, never inside a Core turn. Once per attention
        and evidence in this process, whatever its outcome.
        """
        state = snapshot.state
        if self._has_pending_dispatch(state):
            # A read no live worker owns was stopped by a restart or a failed
            # record. Closed as interrupted, by the same rule as any other,
            # and read again below: it is a repeat-safe observation.
            pending = next(item for item in state.attempts
                           if item.disposition is CapabilityAttemptDisposition.PENDING)
            snapshot = self._close_interrupted_dispatch(snapshot)
            state = snapshot.state
            if self._has_pending_dispatch(state):
                return None
            self._evidence_scheduled.discard(self._evidence_reads.pop(pending.call.call_id, None))
        plan = state.execution_plan
        if plan.attention is None or state.status is not GoalStatus.ACTIVE:
            return None
        for call_id in plan.attention.evidence_call_ids:
            key = (state.goal_id, plan.attention_seq, call_id)
            if (call_id in self._plan_evidence_cache or key in self._evidence_scheduled
                    or not self._needs_reread(state, call_id)):
                continue
            stored = self._attempt_for(state, call_id)
            call = replace(stored.call, call_id=f"plan-evidence:{uuid4()}")
            pending = CapabilityAttempt(call, CapabilityAttemptDisposition.PENDING, None,
                                        reason_code="dispatch_pending")
            checkpoint = self._store.replace(
                replace(state, attempts=(*state.attempts, pending)),
                snapshot.retention_until, snapshot.revision, snapshot.provenance,
            )
            # Scheduled only once its checkpoint is durable: a failed write
            # leaves nothing marked, and the next tick tries again.
            self._evidence_scheduled.add(key)
            self._evidence_reads[call.call_id] = key
            self._live_plan_dispatches.add(call.call_id)
            return PlannedDispatch(
                state.goal_id, checkpoint.conversation_id, plan.plan_id, call, state,
                evidence_for=call_id, attention_seq=plan.attention_seq,
            )
        return None

    def _advance_plan(self, snapshot: GoalSnapshot) -> PlannedDispatch | None:
        """Take one goal's plan as far as it goes without a dispatch."""
        state = snapshot.state
        plan = state.execution_plan
        if plan is None or plan.status in PLAN_TERMINAL:
            return None
        if state.status in (GoalStatus.COMPLETED, GoalStatus.CANCELLED):
            # A finished goal has no work left to run, and no attention left
            # to answer: one objective closure, not a judgment.
            self._write_plan(snapshot, finish_plan(
                plan, PlanStatus.COMPLETED if plan.cursor == len(plan.steps)
                else PlanStatus.CANCELLED,
            ))
            return None
        if plan.status is PlanStatus.NEEDS_CORE:
            return self._evidence_job(snapshot)
        if plan.inflight is not None:
            attempt = self._attempt_for(state, plan.inflight.call_id)
            if attempt.disposition is CapabilityAttemptDisposition.PENDING:
                if plan.inflight.call_id in self._live_plan_dispatches:
                    return None
                if not plan.inflight.started:
                    # Checkpointed, never called: the boundary was not
                    # crossed, so it is dropped and the step simply runs.
                    snapshot = self._finalize_dispatch(
                        snapshot, self._never_called(attempt.call, "dispatch_not_started"),
                        self._clock())
                    snapshot = self._write_plan(snapshot, replace(
                        snapshot.state.execution_plan, inflight=None))
                    state, plan = snapshot.state, snapshot.state.execution_plan
                else:
                    # Started and never recorded: the process stopped. Only
                    # the classifier decides what an interruption means.
                    snapshot = self._finalize_dispatch(
                        snapshot, self._interrupted(attempt.call), self._clock())
            if plan.inflight is not None:
                snapshot = self._reduce_planned_result(snapshot)
                state, plan = snapshot.state, snapshot.state.execution_plan
                if plan.status is not PlanStatus.RUNNING:
                    return None
        now = self._clock()
        if plan.status is PlanStatus.WAITING:
            if plan.wait_deadline is not None and now >= plan.wait_deadline:
                # The bound has passed: no further observation, she is told.
                self._write_plan(snapshot, raise_attention(plan, ("plan_wait_exceeded",), now))
                return None
            if now < plan.next_due_at:
                return None
        if not self._plan_continuation:
            # Nothing could return this plan to her, so nothing is dispatched.
            return None
        if self._has_pending_dispatch(state):
            # A step of a plan she replaced: still running here, or stopped
            # by a restart, in which case it is closed the way a turn closes
            # any interrupted dispatch, and never replayed.
            snapshot = self._close_interrupted_dispatch(snapshot)
            state, plan = snapshot.state, snapshot.state.execution_plan
            if self._has_pending_dispatch(state):
                return None
        facts = plan_invalidation_facts(plan, state, now)
        step = plan.steps[plan.cursor]
        refusal = None if facts else self._plan_dispatch_refusal(snapshot, step, now)
        if facts or refusal is not None:
            self._write_plan(snapshot, raise_attention(
                plan, facts or (refusal,), now))
            return None
        try:
            # The gate a reasoning step would face. Refused, the step waits
            # and is tried again; nothing is checkpointed and she is not woken.
            self._budget_check(snapshot.conversation_id)
        except Exception as error:  # noqa: BLE001 - any stop is a deferral
            LOGGER.info("Planned dispatch deferred by execution budget: %s",
                        type(error).__name__)
            self._write_plan(snapshot, defer_plan(
                plan, now + timedelta(seconds=PLAN_BUDGET_RETRY_SECONDS)))
            return None
        # Each dispatch has its own identity, written to the plan before the
        # call is made. Only a result carrying it can move this plan.
        call = replace(step.call, call_id=f"plan:{uuid4()}")
        approvals = tuple(
            replace(item, lifecycle=ApprovalLifecycle.CLAIMED)
            if (item.lifecycle is ApprovalLifecycle.GRANTED
                and item.approval_id == call.approval_id
                and item.scope.matches(call)) else item
            for item in state.approvals
        )
        pending = CapabilityAttempt(call, CapabilityAttemptDisposition.PENDING, None,
                                    reason_code="dispatch_pending")
        checkpoint = self._store.replace(
            replace(state, attempts=(*state.attempts, pending), approvals=approvals,
                    execution_plan=replace(
                        plan, status=PlanStatus.RUNNING, next_due_at=None,
                        inflight=PlanDispatch(plan.cursor, call.call_id))),
            snapshot.retention_until, snapshot.revision, snapshot.provenance,
        )
        self._live_plan_dispatches.add(call.call_id)
        LOGGER.info("Execution plan %s step=%d %s checkpointed",
                    plan.plan_id, plan.cursor, call.capability_id)
        return PlannedDispatch(
            checkpoint.state.goal_id, checkpoint.conversation_id, plan.plan_id, call,
            # Replaced at the dispatch boundary by the state current then.
            state,
        )

    def _reduce_planned_result(self, snapshot: GoalSnapshot) -> GoalSnapshot:
        """Classify the in-flight dispatch's recorded result once, and apply it."""
        plan = snapshot.state.execution_plan
        assert plan is not None and plan.inflight is not None
        attempt = self._attempt_for(snapshot.state, plan.inflight.call_id)
        step = plan.steps[plan.inflight.step_index]
        now = self._clock()
        classification = classify_planned_result(
            step, attempt, self._definition(step.call.capability_id),
            wait_expired=plan.wait_deadline is not None and now >= plan.wait_deadline,
        )
        reduced = reduce_plan(plan, classification, now,
                              evidence_call_id=attempt.call.call_id)
        if reduced.attention is not None and attempt.result is not None:
            self._plan_evidence_cache[attempt.call.call_id] = attempt
        LOGGER.info("Execution plan %s step=%d %s -> %s %s",
                    plan.plan_id, plan.inflight.step_index, step.call.capability_id,
                    reduced.status.value, ",".join(classification.facts))
        return self._write_plan(snapshot, reduced)

    def _plan_dispatch_refusal(self, snapshot: GoalSnapshot, step,
                               now: datetime) -> str | None:
        """Why the executor may not dispatch this step itself, if it may not."""
        definition = self._definition(step.call.capability_id)
        if definition is None:
            return "plan_capability_unknown"
        if step.wait_seconds and not definition.plan_observation:
            return "plan_wait_unsafe"
        if step.call.capability_id in self._turn_bound_capabilities:
            return "plan_requires_fresh_authority"
        if self._dispatch_blocked_reason(step.call, snapshot.state) is not None:
            return "plan_dispatch_blocked"
        if (self._coding_exhaustion_reason(snapshot.state, step.call) is not None
                or self._coding_interruption_exhaustion_reason(
                    snapshot.state, step.call) is not None):
            return "plan_coding_exhausted"
        if self._binding_rejected_call(snapshot.state, step.call, now) is not None:
            return "plan_repeated_rejection"
        return None

    def _write_plan(self, snapshot: GoalSnapshot, plan: ExecutionPlan) -> GoalSnapshot:
        return self._store.replace(
            replace(snapshot.state, execution_plan=plan),
            snapshot.retention_until, snapshot.revision, snapshot.provenance,
        )

    @staticmethod
    def _attempt_for(state: GoalState, call_id: str) -> CapabilityAttempt:
        return next(item for item in reversed(state.attempts)
                    if item.call is not None and item.call.call_id == call_id)

    @staticmethod
    def _never_called(call: CapabilityCall, reason: str) -> CapabilityAttempt:
        """A checkpoint whose capability was certainly never called.

        Not a refusal: nothing refused it, so it binds no later call. Not
        invoked, so its approval returns and nothing can cite it as evidence.
        """
        return CapabilityAttempt(
            call, CapabilityAttemptDisposition.BROKER_FAILURE, False,
            CapabilityResult(call.call_id, call.capability_id, CapabilityResultState.FAILED,
                             failure={"code": reason}),
            reason,
        )

    @staticmethod
    def _interrupted(call: CapabilityCall) -> CapabilityAttempt:
        return CapabilityAttempt(
            call, CapabilityAttemptDisposition.BROKER_FAILURE, True,
            CapabilityResult(call.call_id, call.capability_id, CapabilityResultState.FAILED,
                             failure={"code": "dispatch_interrupted"}),
            "dispatch_interrupted",
        )

    @staticmethod
    def _plan_is_executing(state: GoalState) -> bool:
        plan = state.execution_plan
        return CoreAgent._has_pending_dispatch(state) or (
            plan is not None and plan.status in (PlanStatus.RUNNING, PlanStatus.WAITING)
        )

    @staticmethod
    def _rendered_plan(state: GoalState | None) -> tuple[str, int] | None:
        """The plan, and its attention, a reasoning step was shown in full."""
        plan = None if state is None else state.execution_plan
        if plan is None:
            return None
        return plan.plan_id, plan.attention_seq

    def _plan_evidence(
        self, snapshot: GoalSnapshot,
    ) -> tuple[GoalSnapshot, tuple[CapabilityAttempt, ...]]:
        """The results the plan's attention asks her to judge, in full.

        The full result if it arrived in this process, or was read again on a
        background worker after a restart; otherwise the durable record,
        which carries only metadata. Nothing is dispatched here: a Core turn
        never waits on an observation.
        """
        plan = snapshot.state.execution_plan
        if plan is None or plan.attention is None:
            return snapshot, ()
        evidence = []
        for call_id in plan.attention.evidence_call_ids:
            cached = self._plan_evidence_cache.get(call_id)
            expires = (None if cached is None or cached.result is None
                       or cached.result.provenance is None
                       else cached.result.provenance.content_expires_at)
            if cached is not None and (expires is None or expires > self._clock()):
                evidence.append(cached)
                continue
            self._plan_evidence_cache.pop(call_id, None)
            stored = next((item for item in snapshot.state.attempts
                           if item.call is not None and item.call.call_id == call_id), None)
            if stored is not None:
                evidence.append(stored)
        return snapshot, tuple(evidence)

    def _plan_update_refusal(
        self, update: PlanUpdate, snapshot: GoalSnapshot | None,
        rendered: tuple[str, int] | None, proposal_error: str | None,
        mechanical_blocker: str | None,
    ) -> str | None:
        """Why a plan change she proposed cannot be applied, if it cannot.

        Resume, accept and finish answer one attention, so they need the step that
        proposed them to have been shown that exact attention. Cancel needs
        it to have been shown that plan. Install needs an active goal, and a
        plan she can see if it replaces one.
        """
        operation = update.operation
        runs_on = (PlanOperation.INSTALL, PlanOperation.RESUME, PlanOperation.ACCEPT)
        if not self._plan_continuation and operation in runs_on:
            return "plan_continuation_unavailable"
        if snapshot is None:
            return "plan_requires_goal"
        if proposal_error is not None:
            return "plan_goal_mutation_rejected"
        if mechanical_blocker is not None and operation in runs_on:
            return mechanical_blocker
        current = snapshot.state.execution_plan
        open_plan = current is not None and current.status not in PLAN_TERMINAL
        if operation is PlanOperation.INSTALL:
            if snapshot.state.status is not GoalStatus.ACTIVE:
                return "plan_requires_active_goal"
            if open_plan and (rendered is None or rendered[0] != current.plan_id):
                return "plan_replaces_unseen_plan"
            for step in update.plan.steps:
                definition = self._definition(step.call.capability_id)
                if definition is None:
                    return "plan_capability_unknown"
                if step.call.capability_id in self._turn_bound_capabilities:
                    return "plan_requires_fresh_authority"
                if step.wait_seconds and not definition.plan_observation:
                    return "plan_wait_unsafe"
            return None
        if not open_plan or rendered is None or rendered[0] != current.plan_id:
            return "plan_not_current"
        if operation is PlanOperation.CANCEL:
            return None
        if (current.status is not PlanStatus.NEEDS_CORE
                or rendered[1] != current.attention_seq):
            return "plan_attention_not_current"
        if operation in (PlanOperation.RESUME, PlanOperation.ACCEPT):
            if snapshot.state.status is not GoalStatus.ACTIVE:
                return "plan_requires_active_goal"
            if current.cursor + int(operation is PlanOperation.ACCEPT) >= len(current.steps):
                return "plan_has_no_remaining_steps"
        if operation is PlanOperation.ACCEPT and not self._judged_observation(
                snapshot.state, current):
            return "plan_step_not_acceptable"
        return None

    def _judged_observation(self, state: GoalState, plan: ExecutionPlan) -> bool:
        """Whether accepting may move the plan past its current step.

        Only when that step already happened and only her judgment remains:
        the classifier raised the attention for completed evidence needing
        judgment, from the current step's own executed dispatch of a declared
        plan observation. Nothing else completed: a failure, a refusal, an
        interruption, or a consequential call such as a merge that reported
        it needs judgment rather than having happened. The result itself is
        not read again here; the classifier is its one interpreter.
        """
        attention = plan.attention
        if (attention is None or attention.reason != "planned_evidence_requires_judgement"
                or len(attention.evidence_call_ids) != 1):
            return False
        step = plan.steps[plan.cursor]
        definition = self._definition(step.call.capability_id)
        attempt = next((item for item in state.attempts if item.call is not None
                        and item.call.call_id == attention.evidence_call_ids[0]), None)
        return (definition is not None and definition.plan_observation
                and attempt is not None
                and attempt.call.capability_id == step.call.capability_id
                and attempt.disposition is CapabilityAttemptDisposition.EXECUTED)

    def _apply_plan_update(
        self, snapshot: GoalSnapshot, update: PlanUpdate,
        conversation: ConversationSnapshot, provenance: ContentProvenance,
        reply: str | None = None,
    ) -> tuple[GoalSnapshot, str | None]:
        """Apply a plan change `_plan_update_refusal` accepted. One write.

        A finish or cancel she answered in words carries those words, under a
        fixed turn id, in the same write: the conversation is another store,
        and this is what lets the reply be stored exactly once whatever
        fails between the two. Returns that turn id, if any.
        """
        current = snapshot.state.execution_plan
        if update.operation is PlanOperation.INSTALL:
            plan = replace(
                update.plan,
                # Each installation has its own identity, though she may reuse
                # a plan_id when she revises one.
                plan_id=f"{update.plan.plan_id}:{uuid4()}",
                # What the plan serves and answers is the runtime's record,
                # never the model's restatement of it.
                objective_source=snapshot.state.objective.source_reference,
                objective_summary=snapshot.state.objective.summary,
                source_turn_id=next(
                    (item.turn_id for item in reversed(conversation.turns)
                     if item.person_id is not None), None),
            )
        elif update.operation in (PlanOperation.RESUME, PlanOperation.ACCEPT):
            plan = resume_plan(current,
                               accept_current=update.operation is PlanOperation.ACCEPT)
        else:
            plan = finish_plan(current, PlanStatus.COMPLETED
                               if update.operation is PlanOperation.FINISH
                               else PlanStatus.CANCELLED)
            if reply is not None:
                plan = replace(plan, announcement=PlanAnnouncement(
                    f"alx-plan-reply:{uuid4()}", reply))
        if current is not None and current.attention is not None:
            for call_id in current.attention.evidence_call_ids:
                self._plan_evidence_cache.pop(call_id, None)
        LOGGER.info("Execution plan %s: %s", plan.plan_id, update.operation.value)
        snapshot = self._store.replace(
            replace(snapshot.state, execution_plan=plan),
            snapshot.retention_until, snapshot.revision, provenance,
        )
        return snapshot, None if plan.announcement is None else plan.announcement.turn_id

    def pending_plan_announcements(self) -> tuple[GoalSnapshot, ...]:
        """Goals whose finish or cancel reply is not yet known to be stored."""
        found = []
        for goal_id in self._store.list_plan_announcement_goal_ids():
            try:
                found.append(self._store.load(goal_id))
            except Exception as error:  # noqa: BLE001 - one goal must not hide the rest
                LOGGER.warning("Plan announcement unreadable for goal %s: %s",
                               goal_id, type(error).__name__)
        return tuple(found)

    def plan_announcement_stored(self, goal_id: str, turn_id: str) -> None:
        """The reply is in the conversation: the plan no longer holds it."""
        snapshot = self._store.load(goal_id)
        plan = snapshot.state.execution_plan
        if plan is None or plan.announcement is None or plan.announcement.turn_id != turn_id:
            return
        self._write_plan(snapshot, replace(plan, announcement=None))

    def _commit_memories(self, snapshot: GoalSnapshot | None,
                         proposals: tuple[MemoryProposal, ...],
                         retention_until: datetime) -> tuple[GoalSnapshot | None, bool]:
        """Persist a batch. False means a mechanical failure, not a conflict.

        `MemoryIdentityConflict` is raised for the caller to hand back to the
        Core; it means the identifier already names different content, which
        is a question about meaning rather than a storage fault.
        """
        if not proposals:
            return snapshot, True
        if self._memory_store is None:
            return snapshot, False
        try:
            conflicts = self._conflicting_memories(proposals, retention_until)
            if conflicts:
                raise MemoryIdentityConflict(str(conflicts[0]["memory_id"]))
            if snapshot is None:
                self._memory_store.remember_many(proposals, retention_until)
                return None, True
            updated = self._store.replace_with_memory_batch(
                snapshot.state,
                snapshot.retention_until,
                snapshot.revision,
                proposals,
                snapshot.provenance,
            )
            return updated, self._flush_pending_memory_batches(updated.state.goal_id)
        except MemoryIdentityConflict:
            raise
        except Exception:
            return snapshot, False

    @property
    def last_goal_rejection(self) -> Mapping[str, Any] | None:
        """Inspect the last refused response in memory, respecting source expiry.

        This is debug state, never a reasoning input or delivered response.
        Restart discards it; persistent logs contain only content-free metadata.
        """
        if self._last_goal_rejection is None:
            return None
        record, provenance = self._last_goal_rejection
        if provenance.is_expired(self._clock()):
            record.pop("proposed_response", None)
        return dict(record)

    def _record_rejection(self, conversation: ConversationSnapshot,
                          decision: AgentDecision, reason: str,
                          now: datetime, provenance: ContentProvenance) -> None:
        """Capture the answer transiently; persist only rejection metadata.

        Objective and criteria prose and hidden reasoning remain excluded.
        """
        proposal = decision.goal_proposal
        if proposal is None:
            return
        references: list[str] = []
        for item in proposal.new_evidence:
            references.extend(item.source_references)
        history_references = [
            reference
            for record in (
                *proposal.new_decisions,
                *proposal.new_corrections,
                *proposal.new_progress,
            )
            for reference in record.evidence_refs
        ]
        record = {
            "conversation_id": conversation.conversation_id,
            "reason": reason,
            "source_references": references,
            "history_evidence_references": history_references,
            "evidence_ids": [item.evidence_id for item in proposal.new_evidence],
            "mutation_kind": proposal.kind.value,
            "proposed_response_present": decision.response is not None,
            "response_requires_goal_commit": decision.response_requires_goal_commit,
            "recorded_at": now.isoformat(),
            "response_content_expires_at": (
                None if provenance.content_expires_at is None
                else provenance.content_expires_at.isoformat()
            ),
        }
        self._last_goal_rejection = (
            {**record, "proposed_response": decision.response}, provenance,
        )
        try:
            self._record_goal_rejection(record)
        except Exception:
            # Diagnosis must never break the turn it is diagnosing.
            LOGGER.warning("Goal rejection record could not be written")

    def _conflicting_memories(
        self, proposals: tuple[MemoryProposal, ...], retention_until: datetime,
    ) -> tuple[Mapping[str, Any], ...]:
        """The mechanical facts the Core needs to resolve an identifier clash.

        Only what is already true in the store: the identifier, the kind, and
        the content it currently holds. Nothing here suggests what she should
        do about it, and nothing compares the two texts for similarity.  The
        equality check deliberately mirrors the durable store's idempotency
        rule, so an exact replay reaches the store harmlessly while every
        changed identity is returned to the Core before durable staging.
        """
        if self._memory_store is None:
            return ()
        conflicts: list[Mapping[str, Any]] = []
        for proposal in proposals:
            try:
                existing = self._memory_store.load(proposal.memory_id)
            except Exception:
                continue
            initial = existing.revisions[0]
            if (
                existing.kind is proposal.kind
                and existing.person_id == proposal.person_id
                and existing.supersedes_memory_id == proposal.supersedes_memory_id
                and initial.content == proposal.content
                and initial.source_references == proposal.source_references
                and initial.recorded_at == proposal.formed_at
                and initial.meaning == proposal.meaning
                and existing.retention_until == retention_until
            ):
                continue
            revision = existing.revisions[-1]
            conflicts.append({
                "memory_id": proposal.memory_id,
                "existing_kind": existing.kind.value,
                "existing_content": revision.content,
                "existing_supersedes_memory_id": existing.supersedes_memory_id,
            })
        return tuple(conflicts)

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
    def _derived_provenance(
        recorded_at: datetime,
        conversation: ConversationSnapshot,
        snapshot: GoalSnapshot | None,
        memories: tuple[MemorySnapshot, ...],
        transient_attempts: tuple[CapabilityAttempt, ...],
        prior_goal_provenance: tuple[ContentProvenance, ...] = (),
    ) -> ContentProvenance:
        """Mechanically union every durable and transient reasoning input."""
        inputs: list[ContentProvenance] = [
            item
            for item in (
                *(turn.provenance for turn in conversation.turns),
                *(event.provenance for event in conversation.events),
                None if snapshot is None else snapshot.provenance,
                *prior_goal_provenance,
                *(memory.current.provenance for memory in memories),
                *(
                    None if attempt.result is None else attempt.result.provenance
                    for attempt in transient_attempts
                ),
            )
            if item is not None
        ]
        policy = RetentionPolicy()
        if not inputs:
            return policy.non_mail(ContentOrigin.ALX, recorded_at)
        return policy.derive(ContentOrigin.ALX, recorded_at, inputs)

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
            # The same rule evidence grounding applies. A memory citing a
            # call that has not run would persist a durable claim about
            # something that never happened, which outlives the turn that
            # made it.
            references.update(
                f"attempt:{item.call.call_id}"
                for item in state.attempts
                if CoreAgent._attempt_is_citable_evidence_source(item)
            )
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
        return None

    @staticmethod
    def _memory_query_is_authorized(conversation: ConversationSnapshot,
                                    query: MemoryQuery) -> bool:
        if query.person_id is None:
            return True
        user_turns = [item for item in conversation.turns
                      if item.origin.value != "alx_response"]
        return bool(user_turns) and user_turns[-1].person_id == query.person_id

    # Category A recovery: a decision refused for a correctable slip in its own
    # fields. Nothing was read, written or dispatched under it, so the state
    # she reasons from next is the state she reasoned from just now plus the
    # name of what was wrong. The existing refused_calls channel carries it,
    # rather than a second retry subsystem beside it.
    #
    # One correction per distinct rejection. Making the same mistake again
    # means reasoning cannot repair it, and the turn stops as it always did.
    @staticmethod
    def _already_refused(
        refused: tuple[Mapping[str, Any], ...], reason: str, subject: str
    ) -> bool:
        return any(
            item.get("reason") == reason and item.get("subject") == subject
            for item in refused
        )

    @staticmethod
    def _call_id_exists(state: GoalState, call_id: str) -> bool:
        return any((item.call is not None and item.call.call_id == call_id)
                   or (item.call is None and item.result is not None
                   and item.result.call_id == call_id) for item in state.attempts)

    # Rejections a later step can genuinely repair by supplying a real
    # approval. In each of these the approval itself was the fault - absent,
    # malformed, mis-scoped, mis-bound, reused, or citing the wrong turn - the
    # action never ran, and nothing about it is settled. A fresh valid
    # approval changes the authority state, so the retry is the correction
    # this guard should elicit rather than punish.
    #
    # Deliberately excluded, because a new approval does not touch the cause:
    # `active_goal_required` (the goal, not the approval), `policy_missing`,
    # `policy_denied` and `permission_missing` (authority configuration),
    # `input_invalid` (the arguments), `approval_covers_unheard_text` (the
    # content), and `approval_capability_already_dispatched` - that last one
    # is the once-per-instruction rule, and bypassing it on a fresh approval
    # is exactly the double-send it exists to prevent.
    _CORRECTABLE_REJECTION_REASONS = frozenset({
        # The ordinary first-time refusal of a consequential action: the call
        # carried no approval because none had been given yet. Asking Friedl
        # and retrying with what he then granted is the whole point of that
        # refusal, so it must not also be the thing that forbids the retry.
        # On 2026-09-11 he answered "yes please" to a draft-bill deletion and
        # the turn died without a word, exactly as the approval_invalid case
        # below had died the day before.
        "approval_required",
        "approval_invalid",
        "approval_call_id_mismatch",
        "approval_scope_mismatch",
        "approval_id_reused",
        "approval_source_missing",
        "approval_source_not_latest_person_turn",
    })

    @classmethod
    def _repeats_rejected_call(
        cls, state: GoalState, call: CapabilityCall, at: datetime
    ) -> bool:
        return cls._binding_rejected_call(state, call, at) is not None

    @classmethod
    def _binding_rejected_call(
        cls, state: GoalState, call: CapabilityCall, at: datetime
    ) -> CapabilityAttempt | None:
        """Stop deterministic safety/input rejections from becoming model loops.

        The identity of a repeat is the capability and its arguments. A fresh
        call or approval identifier does not by itself make a refused action
        different, so retrying with new identifiers alone is still a loop.

        A refusal whose mechanical predicate has since changed is not a loop.
        Approval refusals require a newly valid approval. Coding fuse refusals
        are re-evaluated against the current durable attempts, including the
        historical timeout classification. All other refusals remain binding.

        The approval still has to be real: `permits` re-checks lifecycle,
        identifier, expiry and scope against this call.
        """
        repeats = [
            item
            for item in state.attempts
            if item.call is not None
            and item.disposition is CapabilityAttemptDisposition.REJECTED
            and item.call.capability_id == call.capability_id
            and item.call.arguments == call.arguments
            and not (
                call.capability_id == _RUN_CODING_TASK
                and item.reason_code in {
                    "coding_retry_exhausted", "coding_planning_exhausted",
                    "coding_interruption_no_progress", "coding_interruption_exhausted",
                    *_CODING_CORRECTION_REFUSALS,
                }
                and item.reason_code not in {
                    cls._coding_exhaustion_reason(state, call),
                    cls._coding_interruption_exhaustion_reason(state, call),
                }
            )
        ]
        if not repeats:
            return None
        binding = next(
            (item for item in reversed(repeats)
             if item.reason_code not in cls._CORRECTABLE_REJECTION_REASONS),
            None,
        )
        if binding is not None:
            # A refusal this call cannot repair stands, whatever else happened.
            return binding
        if call.approval_id is None:
            return repeats[-1]
        if any(item.call.approval_id == call.approval_id for item in repeats):
            # The same approval identifier that was already refused.
            return repeats[-1]
        if any(
            approval.approval_id == call.approval_id
            and approval.permits(call, at)
            for approval in state.approvals
        ):
            return None
        return repeats[-1]

    @staticmethod
    def _completed_unchanged_coding_result(item: CapabilityAttempt) -> bool:
        """Recognise durable no-op evidence recorded before the neutral status existed."""
        result = item.result
        if result is None or result.failure is None:
            return False
        values = result.durable_values
        baseline = values.get("baseline")
        try:
            checkpoint = json.loads(values.get("checkpoint"))
        except (TypeError, ValueError):
            return False
        return (
            result.failure.get("code") == "task_failed"
            and result.failure.get("phase") == "test"
            and result.failure.get("session_completed") is True
            and values.get("status") == "failed"
            and values.get("file_count") == 0
            and values.get("diff_digest") == hashlib.sha256(b"").hexdigest()
            and isinstance(baseline, Mapping)
            and baseline.get("clean") is True
            and isinstance(baseline.get("head_sha"), str)
            and len(baseline["head_sha"]) in (40, 64)
            and baseline.get("inherited_dirty") == ()
            and isinstance(checkpoint, dict)
            and checkpoint.get("branch") == baseline.get("branch")
            and checkpoint.get("head_sha") == baseline.get("head_sha")
            and checkpoint.get("stage") == "test"
            and checkpoint.get("files") == []
            and checkpoint.get("preexisting_dirty") == []
            and isinstance(checkpoint.get("state_digest"), str)
            and len(checkpoint["state_digest"]) == 64
            and values.get("all_required_verification_passed") is True
        )

    @staticmethod
    def _is_coding_execution_failure(item: CapabilityAttempt) -> bool:
        """A durable Coding Agent run that failed after reaching implementation."""
        failure = (item.result.failure or {}) if item.result is not None else {}
        return (
            item.call is not None
            and item.call.capability_id == _RUN_CODING_TASK
            and item.implementation_invoked is True
            and item.result is not None
            and item.result.state is CapabilityResultState.FAILED
            and failure.get("code") != "arguments_unusable"
            and failure.get("code") != "planning_failed"
            and failure.get("phase") != "planning"
            and failure.get("code") != "coding_cancelled"
            # Historical fixed-wall-clock timeouts are interruptions too. A
            # pre-watchdog checkpoint can still be resumed on its exact tree.
            and not (
                failure.get("code") == "session_failed"
                and failure.get("reason_code") == "session_timeout"
            )
            and not (
                failure.get("code") == "review_failed"
                and failure.get("review_classification") == "infrastructure"
            )
            and failure.get("failure_class") not in {
                "test_infrastructure", "commit_infrastructure"
            }
            and not CoreAgent._completed_unchanged_coding_result(item)
            # A checkout refused before the feature branch existed: nothing
            # was implemented, so nothing of the allowance was spent.
            and failure.get("implementation_reached") is not False
        )

    @staticmethod
    def _coding_execution_failures(state: GoalState) -> list[Mapping[str, Any]]:
        """Failures of durable Coding Agent runs that reached implementation."""
        return [
            item.result.failure or {}
            for item in state.attempts
            if CoreAgent._is_coding_execution_failure(item)
        ]

    @staticmethod
    def _corrective_action(call: CapabilityCall) -> str:
        """The diagnosis a call carries; blank or absent means a plain retry."""
        value = call.arguments.get(_CORRECTIVE_ACTION)
        return " ".join(value.split()) if isinstance(value, str) else ""

    @staticmethod
    def _is_corrective_coding_dispatch(item: CapabilityAttempt) -> bool:
        """A correction AL/X dispatched that reached the preserved implementation."""
        failure = (item.result.failure or {}) if item.result is not None else {}
        return (
            item.call is not None
            and item.call.capability_id == _RUN_CODING_TASK
            and CoreAgent._corrective_action(item.call)
            and item.implementation_invoked is True
            and item.result is not None
            and failure.get("code") != "arguments_unusable"
            and failure.get("implementation_reached") is not False
        )

    @classmethod
    def _open_coding_failure_episode(
        cls, state: GoalState
    ) -> list[CapabilityAttempt]:
        """Implementation failures since the last success or dispatched correction.

        Retrying without a new diagnosis is a repeat against the same open
        episode, however the call is worded and whatever noise its run
        produced. A succeeded job resolves the episode. A correction AL/X
        dispatched against a recorded failure closes it; that correction's
        own failure, if any, opens the next one. Interruptions, refusals,
        planning failures and request conflicts neither open nor close one.
        """
        episode: list[CapabilityAttempt] = []
        for item in state.attempts:
            if item.call is None or item.call.capability_id != _RUN_CODING_TASK:
                continue
            if (
                item.implementation_invoked is True
                and item.result is not None
                and item.result.state is CapabilityResultState.SUCCEEDED
            ) or cls._is_corrective_coding_dispatch(item):
                episode = []
            if (
                cls._is_coding_execution_failure(item)
                and (item.result.failure or {}).get("failure_class") != "request_conflict"
            ):
                episode.append(item)
        return episode

    @classmethod
    def _failed_coding_executions(cls, state: GoalState) -> int:
        """Genuine implementation failures in the open failure episode."""
        return len(cls._open_coding_failure_episode(state))

    @staticmethod
    def _coding_failure_signature(item: CapabilityAttempt) -> str:
        """What failed, independent of wording and of the tree it failed on.

        The failure code, phase, class and stage, each failed required check,
        and the test identifiers it recorded. A correction that changes the
        tree but leaves the same checks failing has not changed the failure.
        """
        result = item.result
        failure = (result.failure or {}) if result is not None else {}
        values = result.durable_values if result is not None else {}
        failed_checks: list[list[object]] = []
        verification = values.get("verification")
        if isinstance(verification, Mapping):
            for check in verification.get("checks") or ():
                if isinstance(check, Mapping) and not check.get("passed"):
                    failed_checks.append([
                        str(check.get("name", "")),
                        sorted(str(finding) for finding in check.get("findings") or ()),
                    ])
        try:
            checkpoint = json.loads(values.get("checkpoint"))
        except (TypeError, ValueError):
            checkpoint = None
        return json.dumps({
            "code": failure.get("code"),
            "phase": failure.get("phase"),
            "reason_code": failure.get("reason_code"),
            "failure_class": failure.get("failure_class"),
            "stage": checkpoint.get("stage") if isinstance(checkpoint, dict) else None,
            "failed_checks": sorted(failed_checks),
        }, sort_keys=True, default=str)

    @classmethod
    def _coding_correction_refusal(
        cls, state: GoalState, call: CapabilityCall
    ) -> str | None:
        """Whether a correction answers new failure evidence within its bound.

        It must name a failure in the open episode, so each correction answers
        evidence recorded since the last one. At most two corrections may
        answer the same failure signature, and the same diagnosis may not be
        dispatched twice against it. Whether a diagnosis is right, or differs
        enough in substance, is AL/X's judgement and not decided here.
        """
        episode = cls._open_coding_failure_episode(state)
        target = next(
            (item for item in reversed(episode)
             if item.call is not None
             and item.call.call_id == call.arguments.get("resume_job_id")),
            None,
        )
        if target is None:
            return "coding_correction_unanchored"
        signature = cls._coding_failure_signature(target)
        attempted = {
            item.call.call_id: item for item in state.attempts
            if item.call is not None and item.call.capability_id == _RUN_CODING_TASK
        }
        previous = [
            item for item in state.attempts
            if cls._is_corrective_coding_dispatch(item)
            and (anchor := attempted.get(item.call.arguments.get("resume_job_id"))) is not None
            and cls._coding_failure_signature(anchor) == signature
        ]
        diagnosis = cls._corrective_action(call).casefold()
        if any(
            cls._corrective_action(item.call).casefold() == diagnosis
            for item in previous
        ):
            return "coding_correction_repeated"
        if len(previous) >= _MAX_CORRECTIONS_PER_FAILURE:
            return "coding_correction_exhausted"
        return None

    @staticmethod
    def _coding_interruption_exhaustion_reason(
        state: GoalState, call: CapabilityCall
    ) -> str | None:
        """Bound one resumed job without spending the implementation fuse."""
        resume_id = call.arguments.get("resume_job_id")
        if not isinstance(resume_id, str):
            return None
        attempts = {
            item.call.call_id: item for item in state.attempts
            if item.call is not None and item.call.capability_id == _RUN_CODING_TASK
            and item.result is not None
        }
        visited: set[str] = set()
        interrupted = 0
        child_checkpoint: Mapping[str, Any] | None = None
        child_interrupted = False
        while resume_id in attempts and resume_id not in visited:
            visited.add(resume_id)
            attempt = attempts[resume_id]
            result = attempt.result
            if result is None:
                break
            values = result.durable_values
            legacy_timeout = (
                result.state is CapabilityResultState.FAILED
                and (result.failure or {}).get("code") == "session_failed"
                and (result.failure or {}).get("reason_code") == "session_timeout"
            )
            is_interrupted = values.get("status") == "interrupted" or legacy_timeout
            if is_interrupted:
                interrupted += 1
            try:
                checkpoint = json.loads(values.get("checkpoint", ""))
            except (TypeError, ValueError):
                checkpoint = None
            if (
                child_interrupted and is_interrupted
                and child_checkpoint is not None and isinstance(checkpoint, dict)
                and child_checkpoint.get("stage") == checkpoint.get("stage")
                and child_checkpoint.get("head_sha") == checkpoint.get("head_sha")
                and child_checkpoint.get("state_digest") == checkpoint.get("state_digest")
            ):
                return "coding_interruption_no_progress"
            child_checkpoint = checkpoint if isinstance(checkpoint, dict) else None
            child_interrupted = is_interrupted
            parent = attempt.call.arguments.get("resume_job_id")
            if not isinstance(parent, str):
                break
            resume_id = parent
        if interrupted >= _MAX_INTERRUPTED_CODING_EXECUTIONS_PER_JOB:
            return "coding_interruption_exhausted"
        return None

    @classmethod
    def _request_conflict_coding_executions(cls, state: GoalState) -> int:
        """Count runs whose verification only the request's constraints blocked."""
        return sum(
            1 for failure in cls._coding_execution_failures(state)
            if failure.get("failure_class") == "request_conflict"
        )

    @staticmethod
    def _planning_coding_failures(state: GoalState) -> int:
        """Count jobs classified as exhausted deterministic plan validation."""
        return sum(
            1
            for item in state.attempts
            if item.call is not None
            and item.call.capability_id == _RUN_CODING_TASK
            and item.implementation_invoked is True
            and item.result is not None
            and item.result.state is CapabilityResultState.FAILED
            and (item.result.failure or {}).get("code") == "planning_failed"
        )

    @classmethod
    def _coding_exhaustion_reason(
        cls, state: GoalState, call: CapabilityCall | None = None
    ) -> str | None:
        if call is not None and cls._corrective_action(call):
            refusal = cls._coding_correction_refusal(state, call)
            if refusal is not None:
                return refusal
        elif cls._failed_coding_executions(state) >= _MAX_FAILED_CODING_EXECUTIONS:
            return "coding_retry_exhausted"
        if cls._planning_coding_failures(state) >= _MAX_PLANNING_CODING_FAILURES:
            return "coding_planning_exhausted"
        if (
            cls._request_conflict_coding_executions(state)
            >= _MAX_REQUEST_CONFLICT_CODING_EXECUTIONS
        ):
            return "coding_retry_exhausted"
        return None

    @staticmethod
    def _coding_retry_already_exhausted(
        state: GoalState, reason_code: str
    ) -> bool:
        """Whether the refusal is already recorded against the current evidence.

        Only refusals after the latest coding run count. An older refusal is
        evidence about an episode that has since changed, not this one.
        """
        latest = max(
            (index for index, item in enumerate(state.attempts)
             if item.call is not None
             and item.call.capability_id == _RUN_CODING_TASK
             and item.disposition is not CapabilityAttemptDisposition.REJECTED),
            default=-1,
        )
        return any(
            item.call is not None
            and item.call.capability_id == _RUN_CODING_TASK
            and item.reason_code == reason_code
            for item in state.attempts[latest + 1:]
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
        definition = self._definition(call.capability_id)
        if definition is None:
            return decision
        # An approval is redundant when nothing asks for one. That is true of
        # every capability without an outside effect, and equally true of an
        # effectful capability whose policy requires no approval: reading an
        # external review reaches the network but needs only permission.
        #
        # Redundant metadata was not harmless. A volunteered approval is still
        # validated against Friedl's latest turn, and in a background turn the
        # latest turn is AL/X's own response, so the call was refused with
        # approval_source_not_latest_person_turn - and she told him she needed
        # authorisation for a read that never needed any.
        # Only when the composition told this Core which capabilities need an
        # approval can it know that a particular one does not. Without that set
        # nothing is stripped from an effectful call, so a Core built without
        # it behaves exactly as before.
        needs_approval = (
            not self._approval_free_capabilities
            or call.capability_id not in self._approval_free_capabilities
        )
        if definition.side_effect is SideEffect.EFFECTFUL and needs_approval:
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
