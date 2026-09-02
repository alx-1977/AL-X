from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    AgentDecision, ApprovalProposal, ApprovalScope,
    CapabilityAttempt, CapabilityAttemptDisposition,
    CapabilityCall, CapabilityDefinition, CapabilityResult,
    CapabilityResultState, ConversationOrigin, ConversationSnapshot,
    ConversationTurn, DecisionValidationError, Evidence, GoalMutationKind,
    GoalProposal, GoalState, GoalStatus, Objective, SideEffect,
    MemoryKind, MemoryProposal, StructuredSchema, SuccessCriterion, ValueKind,
    WorkItem,
)
from alx.core import CoreAgent, CoreState  # noqa: E402
from alx.core.loop import (  # noqa: E402
    REASONING_TURN_WINDOW,
    project_turns_for_reasoning,
)
from alx.goals import SQLiteGoalStore  # noqa: E402

NOW = datetime(2026, 8, 28, tzinfo=UTC)
RETENTION = NOW + timedelta(days=30)
SCHEMA = StructuredSchema(ValueKind.OBJECT)
DEFINITION = CapabilityDefinition(
    "inspect", "Inspect structured material", SCHEMA, SCHEMA, SideEffect.NONE,
)


def conversation(*turns: ConversationTurn) -> ConversationSnapshot:
    if not turns:
        turns = (ConversationTurn("conversation-1", "turn-1", ConversationOrigin.TYPED,
                                  "Hello", NOW, "friedl"),)
    return ConversationSnapshot("conversation-1", turns, 1, RETENTION)


def goal(**changes) -> GoalState:
    values = dict(
        goal_id="goal-1",
        objective=Objective("turn:turn-1", "Do the work"),
        success_criteria=(SuccessCriterion("criterion-1", "verified"),),
    )
    values.update(changes)
    return GoalState(**values)


class Queued:
    """A fake Core model.

    `selects` is the goal these decisions work under. Nothing attaches a goal
    for the Core any more, so a test exercising an existing goal has its model
    select it, exactly as the real model does from the summaries it is shown.
    """

    def __init__(self, *decisions, selects: str | None = None) -> None:
        self.decisions = list(decisions)
        self.contexts = []
        self._selects = selects

    def decide(self, context):
        self.contexts.append(context)
        item = self.decisions.pop(0)
        if isinstance(item, Exception):
            raise item
        if self._selects is not None and item.goal_id is None:
            item = replace(item, goal_id=self._selects)
        return item


class CoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "goals.sqlite3"
        self.store = SQLiteGoalStore(self.path)

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def agent(self, reasoner, dispatch=lambda call, state: None, identifiers=("goal-1",)):
        values = iter(identifiers)
        return CoreAgent(self.store, reasoner, dispatch, (DEFINITION,),
                         clock=lambda: NOW, identifier_factory=lambda: next(values))

    def test_ordinary_response_requires_no_goal_or_goal_metadata(self) -> None:
        reasoner = Queued(AgentDecision(response="A normal answer."))
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 1)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "A normal answer.")
        self.assertIsNone(outcome.snapshot)
        self.assertIsNone(reasoner.contexts[0].active_goal)
        self.assertEqual(self.store.list_goals(), ())

    def test_core_may_finish_a_general_turn_silently(self) -> None:
        outcome = self.agent(Queued(AgentDecision(finish_silently=True))).process(
            conversation(), RETENTION, 1
        )
        self.assertEqual(outcome.state, CoreState.FINISHED_SILENTLY)
        self.assertEqual(outcome.reason, "core_selected_silence")
        self.assertIsNone(outcome.response)

    def test_silence_cannot_hide_a_required_goal_commit_failure(self) -> None:
        self.store.create(goal(), "conversation-1", RETENTION)
        unsupported = Evidence(
            "evidence-1", "claim", supports=("criterion-1",),
            source_references=("turn:not-real",),
        )
        proposal = GoalProposal(
            GoalMutationKind.REQUEST_COMPLETION,
            new_evidence=(unsupported,),
        )
        outcome = self.agent(Queued(
            AgentDecision(finish_silently=True, goal_proposal=proposal),
            selects="goal-1",
        )).process(conversation(), RETENTION, 1)
        self.assertEqual(outcome.state, CoreState.ERROR)
        self.assertEqual(outcome.reason, "goal_proposal_invalid")

    def test_core_creates_goal_only_from_optional_proposal(self) -> None:
        proposal = GoalProposal(
            GoalMutationKind.CREATE,
            "Investigate the fault",
            (SuccessCriterion("criterion-1", "cause verified"),),
        )
        outcome = self.agent(Queued(AgentDecision(response="I’ll investigate.",
                                                   goal_proposal=proposal))).process(
            conversation(), RETENTION, 1,
        )
        self.assertEqual(outcome.snapshot.state.objective.summary, "Investigate the fault")
        self.assertEqual(outcome.snapshot.conversation_id, "conversation-1")
        self.assertEqual(outcome.snapshot.state.status, GoalStatus.ACTIVE)

    def test_invalid_optional_goal_proposal_does_not_discard_safe_response(self) -> None:
        self.store.create(goal(), "conversation-1", RETENTION)
        unsupported = Evidence(
            "evidence-1", "claim", supports=("criterion-1",),
            source_references=("turn:not-real",),
        )
        proposal = GoalProposal(GoalMutationKind.REQUEST_COMPLETION,
                                new_evidence=(unsupported,))
        reasoner = Queued(AgentDecision(response="Here is the useful answer.",
                                        goal_proposal=proposal), selects="goal-1")
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 2)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "Here is the useful answer.")
        self.assertEqual(outcome.reason, "goal_proposal_rejected")
        self.assertEqual(self.store.load("goal-1").state, goal())
        self.assertEqual(len(reasoner.contexts), 1)

    def test_materially_dependent_rejection_fails_without_blanket_retry(self) -> None:
        self.store.create(goal(), "conversation-1", RETENTION)
        proposal = GoalProposal(GoalMutationKind.REQUEST_COMPLETION)
        reasoner = Queued(
            AgentDecision(response="The goal is complete.", goal_proposal=proposal,
                          response_requires_goal_commit=True),
            AssertionError("blanket retry occurred"),
            selects="goal-1",
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 2)
        self.assertEqual(outcome.reason, "goal_proposal_invalid")
        self.assertEqual(len(reasoner.contexts), 1)

    def test_rejected_memory_cannot_partially_commit_goal_proposal(self) -> None:
        self.store.create(goal(), "conversation-1", RETENTION)
        proposal = GoalProposal(GoalMutationKind.UPDATE,
                                objective_summary="changed objective")
        invalid_memory = MemoryProposal(
            "memory-1", MemoryKind.FACTUAL, "unsupported",
            ("turn:not-real",), NOW,
        )
        outcome = self.agent(Queued(AgentDecision(
            response="response", goal_proposal=proposal,
            memory_proposals=(invalid_memory,),
        ), selects="goal-1")).process(conversation(), RETENTION, 1)
        self.assertEqual(outcome.reason, "memory_proposal_invalid")
        self.assertEqual(self.store.load("goal-1").state.objective.summary,
                         "Do the work")

    def test_completion_is_core_derived_from_sourced_evidence(self) -> None:
        self.store.create(goal(), "conversation-1", RETENTION)
        evidence = Evidence(
            "evidence-1", "observation", supports=("criterion-1",),
            source_references=("turn:turn-1",),
        )
        proposal = GoalProposal(GoalMutationKind.REQUEST_COMPLETION,
                                blockers=(), outstanding_work=(),
                                new_evidence=(evidence,))
        outcome = self.agent(Queued(AgentDecision(response="Verified and complete.",
                                                   goal_proposal=proposal,
                                                   response_requires_goal_commit=True),
                                    selects="goal-1")).process(
            conversation(), RETENTION, 1,
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.snapshot.state.status, GoalStatus.COMPLETED)
        self.assertEqual(outcome.snapshot.state.evidence, (evidence,))

    def test_a_failed_action_cannot_be_cited_as_evidence_it_happened(self) -> None:
        """Evidence must point at something that actually worked.

        The grounding check confirmed an attempt existed but never that it
        succeeded, so a failed save could be cited as proof the save happened
        and the goal would close as complete. AL/X would report work finished
        that no store ever received.
        """
        self.store.create(goal(), "conversation-1", RETENTION)
        call = CapabilityCall("call-1", "inspect", {})
        failed = CapabilityAttempt(
            call, CapabilityAttemptDisposition.EXECUTED, True,
            CapabilityResult("call-1", "inspect", CapabilityResultState.FAILED,
                             {}, {"code": "storage_failed"}),
        )
        claim = GoalProposal(
            GoalMutationKind.REQUEST_COMPLETION,
            new_evidence=(Evidence("evidence-1", "it was recorded",
                                   supports=("criterion-1",),
                                   source_references=("attempt:call-1",)),),
        )
        reasoner = Queued(
            AgentDecision(call=call),
            AgentDecision(response="Recorded.", goal_proposal=claim,
                          response_requires_goal_commit=True),
            selects="goal-1",
        )
        outcome = self.agent(reasoner, lambda proposed, state: failed).process(
            conversation(), RETENTION, 5,
        )
        self.assertEqual(outcome.reason, "goal_proposal_invalid")
        stored = self.store.load("goal-1").state
        self.assertEqual(stored.status, GoalStatus.ACTIVE)
        self.assertEqual(stored.evidence, (), "a false claim must not persist")

    def test_a_partial_action_cannot_prove_completion_either(self) -> None:
        """Half of an action having happened does not make it done."""
        self.store.create(goal(), "conversation-1", RETENTION)
        call = CapabilityCall("call-1", "inspect", {})
        partial = CapabilityAttempt(
            call, CapabilityAttemptDisposition.EXECUTED, True,
            CapabilityResult("call-1", "inspect", CapabilityResultState.PARTIAL,
                             {"written": 1}),
        )
        claim = GoalProposal(
            GoalMutationKind.REQUEST_COMPLETION,
            new_evidence=(Evidence("evidence-1", "it was recorded",
                                   supports=("criterion-1",),
                                   source_references=("attempt:call-1",)),),
        )
        reasoner = Queued(
            AgentDecision(call=call),
            AgentDecision(response="Recorded.", goal_proposal=claim,
                          response_requires_goal_commit=True),
            selects="goal-1",
        )
        outcome = self.agent(reasoner, lambda proposed, state: partial).process(
            conversation(), RETENTION, 5,
        )
        self.assertEqual(outcome.reason, "goal_proposal_invalid")
        self.assertEqual(self.store.load("goal-1").state.status, GoalStatus.ACTIVE)

    def test_a_successful_action_still_completes_the_goal(self) -> None:
        """The guard must not block work that genuinely finished."""
        self.store.create(goal(), "conversation-1", RETENTION)
        call = CapabilityCall("call-1", "inspect", {})
        succeeded = CapabilityAttempt(
            call, CapabilityAttemptDisposition.EXECUTED, True,
            CapabilityResult("call-1", "inspect", CapabilityResultState.SUCCEEDED,
                             {"value": 7}),
        )
        claim = GoalProposal(
            GoalMutationKind.REQUEST_COMPLETION,
            new_evidence=(Evidence("evidence-1", "it was recorded",
                                   supports=("criterion-1",),
                                   source_references=("attempt:call-1",)),),
        )
        reasoner = Queued(
            AgentDecision(call=call),
            AgentDecision(response="Recorded.", goal_proposal=claim,
                          response_requires_goal_commit=True),
            selects="goal-1",
        )
        outcome = self.agent(reasoner, lambda proposed, state: succeeded).process(
            conversation(), RETENTION, 5,
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(
            self.store.load("goal-1").state.status, GoalStatus.COMPLETED
        )

    def test_completion_rejects_outstanding_work_even_with_evidence(self) -> None:
        self.store.create(goal(outstanding_work=(WorkItem("work-1", "verify"),)),
                          "conversation-1", RETENTION)
        evidence = Evidence("evidence-1", "fact", supports=("criterion-1",),
                            source_references=("turn:turn-1",))
        proposal = GoalProposal(GoalMutationKind.REQUEST_COMPLETION,
                                new_evidence=(evidence,))
        outcome = self.agent(Queued(AgentDecision(response="Still working.",
                                                   goal_proposal=proposal),
                                    selects="goal-1")).process(
            conversation(), RETENTION, 1,
        )
        self.assertEqual(outcome.reason, "goal_proposal_rejected")
        self.assertEqual(self.store.load("goal-1").state.status, GoalStatus.ACTIVE)

    def test_tool_result_reenters_same_core_before_response(self) -> None:
        self.store.create(goal(), "conversation-1", RETENTION)
        call = CapabilityCall("call-1", "inspect", {})
        result = CapabilityResult("call-1", "inspect", CapabilityResultState.SUCCEEDED,
                                  {"value": 7})
        attempt = CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED,
                                    True, result)
        reasoner = Queued(
            AgentDecision(call=call),
            AgentDecision(response="I inspected it; more work remains."),
            selects="goal-1",
        )
        outcome = self.agent(reasoner, lambda proposed, state: attempt).process(
            conversation(), RETENTION, 2,
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(reasoner.contexts[1].active_goal.attempts, (attempt,))
        self.assertEqual(outcome.snapshot.state.status, GoalStatus.ACTIVE)

    def test_failed_capability_result_cannot_complete_goal_as_evidence(self) -> None:
        """A failed notebook write cannot prove that persistence succeeded."""
        self.store.create(goal(), "conversation-1", RETENTION)
        call = CapabilityCall("call-1", "inspect", {})
        failed = CapabilityResult(
            "call-1", "inspect", CapabilityResultState.FAILED,
            failure={"code": "storage_failed"},
        )
        attempt = CapabilityAttempt(
            call, CapabilityAttemptDisposition.EXECUTED, True, failed
        )
        false_evidence = Evidence(
            "evidence-1", "notebook_write",
            supports=("criterion-1",),
            source_references=("attempt:call-1",),
        )
        completion = GoalProposal(
            GoalMutationKind.REQUEST_COMPLETION,
            blockers=(),
            outstanding_work=(),
            new_evidence=(false_evidence,),
        )
        reasoner = Queued(
            AgentDecision(call=call),
            AgentDecision(
                finish_silently=True,
                goal_proposal=completion,
            ),
            selects="goal-1",
        )
        outcome = self.agent(
            reasoner, lambda proposed, state: attempt
        ).process(conversation(), RETENTION, 2)
        self.assertEqual(outcome.state, CoreState.ERROR)
        self.assertEqual(outcome.reason, "goal_proposal_invalid")
        recovered = self.store.load("goal-1").state
        self.assertEqual(recovered.status, GoalStatus.ACTIVE)
        self.assertEqual(recovered.evidence, ())

    def test_read_only_tool_can_serve_ordinary_conversation_without_goal(self) -> None:
        call = CapabilityCall("call-1", "inspect", {})
        result = CapabilityResult("call-1", "inspect", CapabilityResultState.SUCCEEDED,
                                  {"value": 7})
        attempt = CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED,
                                    True, result)
        reasoner = Queued(
            AgentDecision(call=call),
            AgentDecision(response="I inspected it."),
        )
        outcome = self.agent(reasoner, lambda proposed, state: attempt).process(
            conversation(), RETENTION, 2,
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "I inspected it.")
        self.assertIsNone(outcome.snapshot)
        self.assertIsNone(reasoner.contexts[1].active_goal)
        self.assertEqual(reasoner.contexts[1].transient_attempts, (attempt,))
        self.assertEqual(self.store.list_goals(), ())

    def test_effectful_tool_still_requires_active_goal(self) -> None:
        """It is refused before anything is recorded, and the turn stops there.

        Continuing into another reasoning step bought nothing: the goalless
        state that made the dispatch impossible is the state the next step
        would decide from, so the loop ran until the step budget was spent.
        One decision, then a checkpoint the next turn can resume from. The
        transport treats the reason as recoverable, so the session stays open.
        """
        effectful = CapabilityDefinition(
            "change", "Change structured material", SCHEMA, SCHEMA,
            SideEffect.EFFECTFUL,
        )
        call = CapabilityCall("call-1", "change", {})
        dispatched = []

        def dispatch(proposed, state):
            dispatched.append(proposed)
            raise AssertionError("an effectful call must not act without a goal")

        reasoner = Queued(
            AgentDecision(call=call),
            AssertionError("a second paid decision occurred"),
        )
        agent = CoreAgent(self.store, reasoner, dispatch, (effectful,), clock=lambda: NOW)
        outcome = agent.process(conversation(), RETENTION, 25)
        self.assertEqual(dispatched, [], "nothing may act without an active goal")
        self.assertEqual(outcome.state, CoreState.CHECKPOINTED)
        self.assertEqual(outcome.reason, "active_goal_required")
        self.assertEqual(len(reasoner.contexts), 1)
        self.assertEqual(self.store.list_goals(), ())

    def test_attention_state_tool_can_serve_ordinary_conversation(self) -> None:
        attention = CapabilityDefinition(
            "release_attention", "Release one attention item", SCHEMA, SCHEMA,
            SideEffect.ATTENTION_STATE,
        )
        call = CapabilityCall("call-1", "release_attention", {})
        result = CapabilityResult(
            "call-1", "release_attention", CapabilityResultState.SUCCEEDED,
            {"released": True},
        )
        attempt = CapabilityAttempt(
            call, CapabilityAttemptDisposition.EXECUTED, True, result,
        )
        reasoner = Queued(
            AgentDecision(call=call),
            AgentDecision(response="I released it from attention."),
        )
        agent = CoreAgent(
            self.store, reasoner, lambda proposed, state: attempt, (attention,),
            clock=lambda: NOW,
        )
        outcome = agent.process(conversation(), RETENTION, 2)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertIsNone(outcome.snapshot)
        self.assertEqual(reasoner.contexts[1].transient_attempts, (attempt,))

    def test_redundant_attention_approval_does_not_break_safe_call(self) -> None:
        attention = CapabilityDefinition(
            "release_attention", "Release one attention item", SCHEMA, SCHEMA,
            SideEffect.ATTENTION_STATE,
        )
        proposed_call = CapabilityCall(
            "call-1", "release_attention", {}, "approval-1"
        )
        issued = CapabilityCall("call-1", "release_attention", {})
        result = CapabilityResult(
            "call-1", "release_attention", CapabilityResultState.SUCCEEDED,
            {"released": True},
        )
        attempt = CapabilityAttempt(
            issued, CapabilityAttemptDisposition.EXECUTED, True, result,
        )
        reasoner = Queued(
            AgentDecision(
                call=proposed_call,
                approval_proposal=ApprovalProposal(
                    "approval-1",
                    ApprovalScope("release_attention", {}),
                    "turn:turn-1",
                ),
            ),
            AgentDecision(response="I released it from attention."),
        )

        def dispatch(call, state):
            self.assertEqual(call, issued)
            self.assertIsNone(state)
            return attempt

        agent = CoreAgent(
            self.store, reasoner, dispatch, (attention,), clock=lambda: NOW,
        )
        outcome = agent.process(conversation(), RETENTION, 2)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(reasoner.contexts[1].transient_attempts, (attempt,))

    def test_provider_validation_error_is_not_retried(self) -> None:
        reasoner = Queued(DecisionValidationError("malformed"),
                          AssertionError("retry occurred"))
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 3)
        self.assertEqual(outcome.reason, "reasoner_error")
        self.assertEqual(len(reasoner.contexts), 1)

    def test_same_deterministic_rejection_cannot_loop_through_dispatch(self) -> None:
        effectful = CapabilityDefinition(
            "change", "Change structured material", SCHEMA, SCHEMA,
            SideEffect.EFFECTFUL,
        )
        self.store.create(goal(), "conversation-1", RETENTION)
        first = CapabilityCall("call-1", "change", {}, "approval-1")
        repeated = CapabilityCall("call-2", "change", {}, "approval-1")
        reasoner = Queued(
            AgentDecision(call=first),
            AgentDecision(call=repeated),
            AssertionError("third model decision occurred"),
            selects="goal-1",
        )
        dispatches = []

        def dispatch(call, state):
            dispatches.append(call)
            return CapabilityAttempt(
                call,
                CapabilityAttemptDisposition.REJECTED,
                False,
                reason_code="approval_invalid",
            )

        agent = CoreAgent(
            self.store, reasoner, dispatch, (effectful,), clock=lambda: NOW,
        )
        outcome = agent.process(conversation(), RETENTION, 3)
        self.assertEqual(outcome.reason, "repeated_rejected_call")
        self.assertEqual(dispatches, [first])
        self.assertEqual(len(reasoner.contexts), 2)

    def test_interrupted_dispatch_recovers_without_repeating_the_action(self) -> None:
        """An interrupted dispatch must neither wedge the goal nor be retried.

        The external action may already have taken effect, so the attempt is
        closed with an explicitly unknown outcome and handed to the Core as
        evidence. Only the Core may decide whether to verify or ask.
        """
        self.store.create(goal(), "conversation-1", RETENTION)
        call = CapabilityCall("call-1", "inspect", {})
        outcome = self.agent(
            Queued(AgentDecision(call=call), selects="goal-1"),
            lambda proposed, state: (_ for _ in ()).throw(RuntimeError()),
        ).process(conversation(), RETENTION, 1)
        self.assertEqual(outcome.reason, "dispatch_error")
        self.store.close()
        self.store = SQLiteGoalStore(self.path)

        dispatches: list[CapabilityCall] = []

        def dispatch(proposed, state):
            dispatches.append(proposed)
            raise AssertionError("an interrupted action must not be re-dispatched")

        reasoner = Queued(AgentDecision(response="I could not confirm that."),
                          selects="goal-1")
        resumed = self.agent(reasoner, dispatch).process(
            conversation(), RETENTION, 1
        )
        # The goal is usable again and the model was consulted.
        self.assertEqual(resumed.state, CoreState.RESPONDED)
        self.assertEqual(len(reasoner.contexts), 1)
        # The interrupted action was never repeated.
        self.assertEqual(dispatches, [])
        # The unknown outcome is durable evidence the Core can reason about.
        stored = self.store.load("goal-1").state
        self.assertFalse(
            any(item.disposition is CapabilityAttemptDisposition.PENDING
                for item in stored.attempts)
        )
        closed = stored.attempts[-1]
        self.assertEqual(closed.reason_code, "dispatch_interrupted")
        self.assertEqual(closed.result.failure["code"], "dispatch_interrupted")


class ReasoningProjectionTests(unittest.TestCase):
    """Send goal-relevant context, never the entire history.

    Replaying all 71 stored turns made each call slower than the last: 35s,
    78s, then 154s before the provider returned a blank response. The
    projection is deterministic — a fixed window plus whatever the active goal
    still cites — so latency is predictable and no model summarisation stands
    between AL/X and what was actually said.
    """

    def turns(self, count: int) -> tuple:
        moment = datetime(2026, 9, 1, tzinfo=UTC)
        return tuple(
            ConversationTurn(
                "conversation-1",
                f"turn-{index}",
                ConversationOrigin.TYPED,
                f"message {index}",
                moment,
                "friedl",
            )
            for index in range(count)
        )

    def test_a_short_conversation_is_sent_whole(self) -> None:
        turns = self.turns(5)
        self.assertEqual(project_turns_for_reasoning(turns, None), turns)

    def test_a_long_conversation_is_windowed(self) -> None:
        kept = project_turns_for_reasoning(self.turns(71), None)
        self.assertEqual(len(kept), REASONING_TURN_WINDOW)
        self.assertEqual(kept[-1].turn_id, "turn-70")

    def test_the_window_keeps_the_most_recent_turns(self) -> None:
        kept = project_turns_for_reasoning(self.turns(40), None)
        self.assertEqual(
            [item.turn_id for item in kept],
            [f"turn-{index}" for index in range(28, 40)],
        )

    def test_original_order_is_preserved(self) -> None:
        kept = project_turns_for_reasoning(self.turns(60), _state("turn:turn-2"))
        ordinals = [int(item.turn_id.split("-")[1]) for item in kept]
        self.assertEqual(ordinals, sorted(ordinals))

    def test_the_turn_a_goal_came_from_is_never_dropped(self) -> None:
        """Cost control may not silently truncate an active goal."""
        kept = project_turns_for_reasoning(self.turns(71), _state("turn:turn-1"))
        self.assertIn("turn-1", {item.turn_id for item in kept})

    def test_evidence_sources_are_kept_however_old(self) -> None:
        state = _state("turn:turn-60", evidence_sources=("turn:turn-3",))
        kept = {item.turn_id for item in project_turns_for_reasoning(self.turns(71), state)}
        self.assertIn("turn-3", kept)

    def test_decisions_corrections_and_approvals_are_kept(self) -> None:
        state = _state(
            "turn:turn-70",
            decisions=("turn:turn-4",),
            corrections=("turn:turn-5",),
            approvals=("turn:turn-6",),
        )
        kept = {item.turn_id for item in project_turns_for_reasoning(self.turns(71), state)}
        for turn_id in ("turn-4", "turn-5", "turn-6"):
            with self.subTest(turn_id=turn_id):
                self.assertIn(turn_id, kept)

    def test_a_non_turn_reference_is_ignored(self) -> None:
        state = _state("evidence:e-1", evidence_sources=("event:x", "evidence:y"))
        self.assertEqual(
            len(project_turns_for_reasoning(self.turns(71), state)),
            REASONING_TURN_WINDOW,
        )

    def test_projection_never_mutates_the_stored_conversation(self) -> None:
        """The complete thread stays stored and unrewritten."""
        turns = self.turns(71)
        before = [item.turn_id for item in turns]
        project_turns_for_reasoning(turns, _state("turn:turn-0"))
        self.assertEqual([item.turn_id for item in turns], before)
        self.assertEqual(len(turns), 71)

    def test_the_core_projects_rather_than_sending_everything(self) -> None:
        """Proof the live path uses it, not merely that it exists."""
        source = (
            Path(__file__).resolve().parents[1] / "src/alx/core/loop.py"
        ).read_text()
        self.assertIn("turns=project_turns_for_reasoning(", source)
        self.assertNotIn("turns=conversation.turns,", source)

    def test_grounding_still_validates_against_the_whole_conversation(self) -> None:
        """Windowing the model's view must not narrow what may be cited."""
        source = (
            Path(__file__).resolve().parents[1] / "src/alx/core/loop.py"
        ).read_text()
        self.assertIn(
            'known = {f"turn:{item.turn_id}" for item in conversation.turns}', source
        )
        self.assertIn(
            'turns = {f"turn:{item.turn_id}": item.person_id '
            "for item in conversation.turns}",
            source,
        )


def _state(
    objective_reference: str,
    *,
    evidence_sources: tuple = (),
    decisions: tuple = (),
    corrections: tuple = (),
    approvals: tuple = (),
):
    """A stand-in carrying only the references the projection reads."""

    class Objective:
        source_reference = objective_reference

    class Evidence:
        source_references = evidence_sources

    def referencing(values):
        return tuple(
            type("Item", (), {"source_reference": value})() for value in values
        )

    class State:
        objective = Objective()
        evidence = (Evidence(),) if evidence_sources else ()
        decisions_ = None

    state = State()
    state.decisions = referencing(decisions)
    state.corrections = referencing(corrections)
    state.approvals = referencing(approvals)
    return state


if __name__ == "__main__":
    unittest.main()
