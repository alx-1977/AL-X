from __future__ import annotations

import inspect
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
    GoalStopReason,
    WorkItem,
)
from alx.contracts import ApprovalProposal, ApprovalScope  # noqa: E402
from alx.contracts.memory import MemoryQuery  # noqa: E402
from alx.core import CoreAgent, CoreState  # noqa: E402
from alx.memories import SQLiteMemoryStore  # noqa: E402
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
# Approval metadata is stripped from a side-effect-free call, so the approval
# refusal guards can only be exercised through an effectful capability.
EFFECTFUL = CapabilityDefinition(
    "remove_item", "Remove one item", SCHEMA, SCHEMA, SideEffect.EFFECTFUL,
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


class CategoryARecoveryTests(unittest.TestCase):
    """Identifier slips are corrected, not fatal.

    A reused call_id killed a live voice turn on 2026-09-10 after the Core had
    otherwise recovered. None of these rejections reads, writes or dispatches
    anything, so the only thing a further step needs is the name of what was
    wrong.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "goals.sqlite3"
        self.store = SQLiteGoalStore(self.path)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.close)

    def _agent(self, reasoner, dispatch):
        return CoreAgent(
            self.store, reasoner, dispatch, (DEFINITION, EFFECTFUL),
            memory_store=SQLiteMemoryStore(Path(self.directory.name) / "m.sqlite3"),
            clock=lambda: NOW, identifier_factory=lambda: "goal-1",
        )

    @staticmethod
    def _executes():
        dispatched: list[CapabilityCall] = []

        def dispatch(call, state):
            dispatched.append(call)
            return CapabilityAttempt(
                call, CapabilityAttemptDisposition.EXECUTED, True,
                CapabilityResult(
                    call.call_id, call.capability_id,
                    CapabilityResultState.SUCCEEDED, {"value": 1},
                ),
            )

        return dispatch, dispatched

    def test_a_reused_call_id_is_corrected_and_a_fresh_one_proceeds(self) -> None:
        """1: the live failure, now recoverable."""
        self.store.create(goal(), "conversation-1", RETENTION)
        dispatch, dispatched = self._executes()
        reasoner = Queued(
            AgentDecision(call=CapabilityCall("call-1", "inspect", {})),
            AgentDecision(call=CapabilityCall("call-1", "inspect", {})),  # reused
            AgentDecision(call=CapabilityCall("call-2", "inspect", {})),  # corrected
            AgentDecision(response="Both done."),
            selects="goal-1",
        )
        outcome = self._agent(reasoner, dispatch).process(conversation(), RETENTION, 8)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "Both done.")
        self.assertEqual([item.call_id for item in dispatched], ["call-1", "call-2"])

    def test_the_correction_names_the_reused_identifier(self) -> None:
        self.store.create(goal(), "conversation-1", RETENTION)
        dispatch, _ = self._executes()
        reasoner = Queued(
            AgentDecision(call=CapabilityCall("call-1", "inspect", {})),
            AgentDecision(call=CapabilityCall("call-1", "inspect", {})),
            AgentDecision(response="Corrected."),
            selects="goal-1",
        )
        self._agent(reasoner, dispatch).process(conversation(), RETENTION, 8)
        refused = reasoner.contexts[-1].refused_calls
        self.assertTrue(any(
            item["reason"] == "call_id_reused" and item["subject"] == "call-1"
            for item in refused
        ))

    def test_reusing_the_same_call_id_again_still_stops(self) -> None:
        """2: told once and repeated, so reasoning cannot repair it."""
        self.store.create(goal(), "conversation-1", RETENTION)
        dispatch, dispatched = self._executes()
        reasoner = Queued(
            AgentDecision(call=CapabilityCall("call-1", "inspect", {})),
            AgentDecision(call=CapabilityCall("call-1", "inspect", {})),
            AgentDecision(call=CapabilityCall("call-1", "inspect", {})),
            AgentDecision(response="unreachable"),
            selects="goal-1",
        )
        outcome = self._agent(reasoner, dispatch).process(conversation(), RETENTION, 8)
        self.assertEqual(outcome.state, CoreState.ERROR)
        self.assertEqual(outcome.reason, "call_id_reused")
        self.assertEqual([item.call_id for item in dispatched], ["call-1"])

    def test_a_rejected_call_id_leaves_no_state_or_tool_effect(self) -> None:
        """5: the rejected decision changed nothing."""
        self.store.create(goal(), "conversation-1", RETENTION)
        dispatch, dispatched = self._executes()
        reasoner = Queued(
            AgentDecision(call=CapabilityCall("call-1", "inspect", {})),
            AgentDecision(call=CapabilityCall("call-1", "inspect", {})),
            AgentDecision(response="Done."),
            selects="goal-1",
        )
        self._agent(reasoner, dispatch).process(conversation(), RETENTION, 8)
        attempts = self.store.load("goal-1").state.attempts
        # One dispatch, one attempt: the refused decision added neither.
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].call.call_id, "call-1")

    def test_a_reused_memory_query_id_is_corrected(self) -> None:
        """3: same shape, memory retrieval."""
        self.store.create(goal(), "conversation-1", RETENTION)
        dispatch, _ = self._executes()
        query = MemoryQuery("q-1", kinds=(MemoryKind.RELATIONSHIP,), person_id="friedl")
        reasoner = Queued(
            AgentDecision(memory_query=query),
            AgentDecision(memory_query=query),  # reused
            AgentDecision(memory_query=MemoryQuery("q-2", kinds=(MemoryKind.RELATIONSHIP,), person_id="friedl")),
            AgentDecision(response="Recalled."),
            selects="goal-1",
        )
        outcome = self._agent(reasoner, dispatch).process(conversation(), RETENTION, 8)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertTrue(any(
            item["reason"] == "memory_query_id_reused"
            for item in reasoner.contexts[-1].refused_calls
        ))

    def test_a_reused_memory_query_id_twice_still_stops(self) -> None:
        self.store.create(goal(), "conversation-1", RETENTION)
        dispatch, _ = self._executes()
        query = MemoryQuery("q-1", kinds=(MemoryKind.RELATIONSHIP,), person_id="friedl")
        reasoner = Queued(
            AgentDecision(memory_query=query),
            AgentDecision(memory_query=query),
            AgentDecision(memory_query=query),
            AgentDecision(response="unreachable"),
            selects="goal-1",
        )
        outcome = self._agent(reasoner, dispatch).process(conversation(), RETENTION, 8)
        self.assertEqual(outcome.state, CoreState.ERROR)
        self.assertEqual(outcome.reason, "memory_query_id_reused")

    def test_an_invalid_memory_proposal_is_corrected(self) -> None:
        """4: the ungrounded proposal is dropped, a valid turn continues."""
        self.store.create(goal(), "conversation-1", RETENTION)
        dispatch, _ = self._executes()
        ungrounded = MemoryProposal(
            "memory-1", MemoryKind.FACTUAL, "unsupported", ("turn:not-real",), NOW,
        )
        reasoner = Queued(
            AgentDecision(response="One.", memory_proposals=(ungrounded,)),
            AgentDecision(response="Corrected."),
            selects="goal-1",
        )
        outcome = self._agent(reasoner, dispatch).process(conversation(), RETENTION, 8)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "Corrected.")
        self.assertTrue(any(
            item["reason"] == "memory_proposal_invalid"
            for item in reasoner.contexts[-1].refused_calls
        ))

    def test_an_invalid_memory_proposal_repeated_still_stops(self) -> None:
        self.store.create(goal(), "conversation-1", RETENTION)
        dispatch, _ = self._executes()
        ungrounded = MemoryProposal(
            "memory-1", MemoryKind.FACTUAL, "unsupported", ("turn:not-real",), NOW,
        )
        reasoner = Queued(
            AgentDecision(response="One.", memory_proposals=(ungrounded,)),
            AgentDecision(response="Two.", memory_proposals=(ungrounded,)),
            AgentDecision(response="unreachable"),
            selects="goal-1",
        )
        outcome = self._agent(reasoner, dispatch).process(conversation(), RETENTION, 8)
        self.assertEqual(outcome.state, CoreState.ERROR)
        self.assertEqual(outcome.reason, "memory_proposal_invalid")

    def test_a_memory_cannot_cite_an_attempt_that_never_ran(self) -> None:
        """Durable memory must not record something that never happened.

        Evidence grounding already refused this. Memory grounding accepted
        every attempt with a call, so a proposal citing a pending or refused
        attempt could persist a claim outliving the turn that made it.
        """
        call = CapabilityCall("call-1", "remove_item", {}, "appr-1")
        # Refused before its implementation ran, so nothing happened under it.
        refused = CapabilityAttempt(
            call, CapabilityAttemptDisposition.REJECTED, False, None,
            reason_code="approval_invalid",
        )
        self.store.create(
            goal(attempts=(refused,)), "conversation-1", RETENTION,
        )
        dispatch, _ = self._executes()
        proposal = MemoryProposal(
            "memory-1", MemoryKind.FACTUAL, "the item was removed",
            ("attempt:call-1",), NOW,
        )
        reasoner = Queued(
            AgentDecision(response="Noted.", memory_proposals=(proposal,)),
            AgentDecision(response="Corrected."),
            selects="goal-1",
        )
        outcome = self._agent(reasoner, dispatch).process(conversation(), RETENTION, 8)
        # Refused, and correctable rather than fatal: the citation is wrong,
        # not the turn.
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertTrue(any(
            item["reason"] == "memory_proposal_invalid"
            for item in reasoner.contexts[-1].refused_calls
        ))

    def test_a_memory_may_cite_an_attempt_that_did_run(self) -> None:
        """The rule narrows nothing that genuinely happened."""
        call = CapabilityCall("call-1", "remove_item", {}, "appr-1")
        executed = CapabilityAttempt(
            call, CapabilityAttemptDisposition.EXECUTED, True,
            CapabilityResult(
                "call-1", "remove_item", CapabilityResultState.SUCCEEDED,
                {"removed": True},
            ),
        )
        self.store.create(
            goal(attempts=(executed,)), "conversation-1", RETENTION,
        )
        dispatch, _ = self._executes()
        proposal = MemoryProposal(
            "memory-1", MemoryKind.FACTUAL, "the item was removed",
            ("attempt:call-1",), NOW,
        )
        reasoner = Queued(
            AgentDecision(response="Noted.", memory_proposals=(proposal,)),
            selects="goal-1",
        )
        outcome = self._agent(reasoner, dispatch).process(conversation(), RETENTION, 8)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(reasoner.contexts[-1].refused_calls, ())

    def test_an_earlier_correction_does_not_swallow_an_approval_refusal(self) -> None:
        """A corrected identifier slip must not hide the next, unrelated refusal.

        The older guards stopped on any existing refused_calls entry. Once an
        identifier slip could be corrected, that entry made the very next
        approval error checkpoint before the Core was ever told what was wrong
        with it.
        """
        self.store.create(goal(), "conversation-1", RETENTION)
        dispatch, dispatched = self._executes()
        reasoner = Queued(
            AgentDecision(call=CapabilityCall("call-1", "inspect", {})),
            # A corrected identifier slip: records a refusal of its own.
            AgentDecision(call=CapabilityCall("call-1", "inspect", {})),
            # An unrelated approval error must still reach her as feedback.
            AgentDecision(
                call=CapabilityCall("call-2", "remove_item", {}, "appr-1"),
                approval_proposal=ApprovalProposal(
                    "appr-mismatch", ApprovalScope("remove_item", {}), "turn:turn-1",
                ),
            ),
            AgentDecision(response="Corrected both."),
            selects="goal-1",
        )
        outcome = self._agent(reasoner, dispatch).process(conversation(), RETENTION, 8)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "Corrected both.")
        reasons = {
            item["reason"] for item in reasoner.contexts[-1].refused_calls
        }
        self.assertIn("call_id_reused", reasons)
        self.assertIn("approval_call_id_mismatch", reasons)

    def test_the_same_approval_refusal_twice_still_checkpoints(self) -> None:
        """Scoping the guard must not disable it."""
        self.store.create(goal(), "conversation-1", RETENTION)
        dispatch, _ = self._executes()
        bad = AgentDecision(
            call=CapabilityCall("call-1", "remove_item", {}, "appr-1"),
            approval_proposal=ApprovalProposal(
                "appr-mismatch", ApprovalScope("remove_item", {}), "turn:turn-1",
            ),
        )
        reasoner = Queued(
            bad,
            AgentDecision(
                call=CapabilityCall("call-2", "remove_item", {}, "appr-2"),
                approval_proposal=ApprovalProposal(
                    "appr-mismatch-2", ApprovalScope("remove_item", {}), "turn:turn-1",
                ),
            ),
            AgentDecision(response="unreachable"),
            selects="goal-1",
        )
        outcome = self._agent(reasoner, dispatch).process(conversation(), RETENTION, 8)
        self.assertEqual(outcome.state, CoreState.CHECKPOINTED)
        self.assertEqual(outcome.reason, "approval_call_id_mismatch")

    def test_the_step_budget_still_bounds_corrections(self) -> None:
        """6: a correction is a step, and the budget still ends the turn."""
        self.store.create(goal(), "conversation-1", RETENTION)
        dispatch, dispatched = self._executes()
        reasoner = Queued(
            AgentDecision(call=CapabilityCall("call-1", "inspect", {})),
            AgentDecision(call=CapabilityCall("call-1", "inspect", {})),
            AgentDecision(response="never reached"),
            selects="goal-1",
        )
        outcome = self._agent(reasoner, dispatch).process(conversation(), RETENTION, 2)
        self.assertIsNone(outcome.response)
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(len(reasoner.contexts), 2)


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

    def test_invalid_optional_proposal_does_not_end_executable_work(self) -> None:
        self.store.create(
            goal(outstanding_work=(WorkItem("work-1", "continue investigation"),)),
            "conversation-1", RETENTION,
        )
        invalid = GoalProposal(
            GoalMutationKind.REQUEST_COMPLETION,
            new_evidence=(Evidence(
                "evidence-1", "unsupported", supports=("criterion-1",),
                source_references=("turn:not-real",),
            ),),
        )
        reasoner = Queued(
            AgentDecision(response="Premature response.", goal_proposal=invalid),
            AgentDecision(
                response="Investigation continued.",
                goal_proposal=GoalProposal(
                    GoalMutationKind.UPDATE, outstanding_work=(),
                ),
            ),
            selects="goal-1",
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 2)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "Investigation continued.")
        self.assertEqual(len(reasoner.contexts), 2)
        self.assertEqual(self.store.load("goal-1").state.outstanding_work, ())

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
        # One step, so the correction has nowhere to go and the turn still
        # stops. What must not happen either way is a partial commit.
        outcome = self.agent(Queued(AgentDecision(
            response="response", goal_proposal=proposal,
            memory_proposals=(invalid_memory,),
        ), selects="goal-1")).process(conversation(), RETENTION, 1)
        self.assertIsNone(outcome.response)
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

    def test_await_input_without_outstanding_work_stays_active(self) -> None:
        """A stale goal in this shape refused every further turn on its thread."""
        self.store.create(goal(), "conversation-1", RETENTION)
        proposal = GoalProposal(GoalMutationKind.AWAIT_INPUT)
        outcome = self.agent(Queued(
            AgentDecision(
                response="That run failed; tell me how you want to proceed.",
                goal_proposal=proposal,
                response_requires_goal_commit=True,
            ),
            selects="goal-1",
        )).process(conversation(), RETENTION, 1)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertNotEqual(outcome.reason, "goal_proposal_invalid")
        state = self.store.load("goal-1").state
        self.assertIs(state.status, GoalStatus.ACTIVE)
        self.assertIsNone(state.stop_reason)
        # No work was invented to satisfy the validator.
        self.assertEqual(state.outstanding_work, ())

    def test_await_input_still_waits_when_work_is_genuinely_outstanding(self) -> None:
        """The real meaning of awaiting input is preserved."""
        self.store.create(
            goal(outstanding_work=(WorkItem("work-1", "which branch?"),)),
            "conversation-1", RETENTION,
        )
        proposal = GoalProposal(GoalMutationKind.AWAIT_INPUT)
        outcome = self.agent(Queued(
            AgentDecision(response="Which branch should I use?",
                          goal_proposal=proposal),
            selects="goal-1",
        )).process(conversation(), RETENTION, 1)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        state = self.store.load("goal-1").state
        self.assertIs(state.status, GoalStatus.AWAITING_INPUT)
        self.assertIs(state.stop_reason, GoalStopReason.REQUIRED_INPUT)

    def test_a_new_instruction_can_dispatch_from_that_state(self) -> None:
        """The stale goal must not block acting on what Friedl asks next."""
        self.store.create(goal(), "conversation-1", RETENTION)
        dispatched: list[CapabilityCall] = []

        def dispatch(call, state):
            dispatched.append(call)
            return CapabilityAttempt(
                call, CapabilityAttemptDisposition.EXECUTED, True,
                CapabilityResult(
                    call.call_id, call.capability_id,
                    CapabilityResultState.SUCCEEDED, {"value": 1},
                ),
            )

        outcome = self.agent(Queued(
            AgentDecision(
                call=CapabilityCall("call-1", "inspect", {}),
                goal_proposal=GoalProposal(GoalMutationKind.AWAIT_INPUT),
            ),
            AgentDecision(response="Here is what I found."),
            selects="goal-1",
        ), dispatch=dispatch).process(conversation(), RETENTION, 2)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual([item.capability_id for item in dispatched], ["inspect"])
        self.assertIs(self.store.load("goal-1").state.status, GoalStatus.ACTIVE)

    def test_a_failed_attempt_cannot_close_the_goal(self) -> None:
        """Failed coding evidence stays failed and cannot complete a goal."""
        call = CapabilityCall("call-1", "inspect", {})
        result = CapabilityResult(
            "call-1", "inspect", CapabilityResultState.FAILED,
            {"status": "failed"},
            failure={"code": "task_failed"},
        )
        attempt = CapabilityAttempt(
            call, CapabilityAttemptDisposition.EXECUTED, True, result,
        )
        self.store.create(goal(attempts=(attempt,)), "conversation-1", RETENTION)
        evidence = Evidence(
            "evidence-1", "the coding job failed", supports=("criterion-1",),
            source_references=("attempt:call-1",),
        )
        proposal = GoalProposal(
            GoalMutationKind.REQUEST_COMPLETION, new_evidence=(evidence,),
        )
        outcome = self.agent(Queued(
            AgentDecision(response="All done.", goal_proposal=proposal),
            selects="goal-1",
        )).process(conversation(), RETENTION, 1)
        self.assertEqual(outcome.reason, "goal_proposal_rejected")
        self.assertIs(self.store.load("goal-1").state.status, GoalStatus.ACTIVE)

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
        self.assertEqual(
            self.store.load("goal-1").state.status, GoalStatus.AWAITING_INPUT
        )

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
            # The refusal returns to her once with its reason; repeating the
            # same impossible call ends the turn.
            AgentDecision(call=CapabilityCall("call-2", "change", {})),
            AssertionError("a third paid decision occurred"),
        )
        agent = CoreAgent(self.store, reasoner, dispatch, (effectful,), clock=lambda: NOW)
        outcome = agent.process(conversation(), RETENTION, 25)
        self.assertEqual(dispatched, [], "nothing may act without an active goal")
        self.assertEqual(outcome.state, CoreState.CHECKPOINTED)
        self.assertEqual(outcome.reason, "active_goal_required")
        self.assertEqual(len(reasoner.contexts), 2)
        self.assertEqual(
            reasoner.contexts[1].refused_calls[0]["reason"], "active_goal_required",
        )
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

    def test_effectful_queue_continues_after_premature_response(self) -> None:
        """A call-less end must not stop a queue that can still run this turn.

        After the first successful item the Core used to accept a response as
        the end of the person turn. The goal stayed ACTIVE, no further
        dispatches ran, and nothing was scheduled that could continue without
        another user turn. Remaining approval-required work also could not
        resume later, because a later turn is no longer grounded in Friedl's
        instruction.
        """
        effectful = CapabilityDefinition(
            "change", "Change structured material", SCHEMA, SCHEMA,
            SideEffect.EFFECTFUL,
        )
        self.store.create(
            goal(outstanding_work=(
                WorkItem("item-1", "first item"),
                WorkItem("item-2", "second item"),
            )),
            "conversation-1",
            RETENTION,
        )

        def approved(call_id: str, item_id: str):
            arguments = {"item_id": item_id}
            call = CapabilityCall(
                call_id, "change", arguments, f"approval-{call_id}",
            )
            return call, ApprovalProposal(
                call.approval_id,
                ApprovalScope("change", arguments),
                "turn:turn-1",
            )

        first, first_approval = approved("call-1", "item-1")
        second, second_approval = approved("call-2", "item-2")
        dispatched: list[str] = []

        def dispatch(call, state):
            dispatched.append(call.call_id)
            return CapabilityAttempt(
                call, CapabilityAttemptDisposition.EXECUTED, True,
                CapabilityResult(
                    call.call_id, call.capability_id,
                    CapabilityResultState.SUCCEEDED, {"ok": True},
                ),
            )

        reasoner = Queued(
            AgentDecision(call=first, approval_proposal=first_approval),
            AgentDecision(
                response="Still working through the rest.",
                goal_proposal=GoalProposal(
                    GoalMutationKind.UPDATE,
                    outstanding_work=(WorkItem("item-2", "second item"),),
                ),
            ),
            AgentDecision(call=second, approval_proposal=second_approval),
            AgentDecision(
                response="Both items are done.",
                goal_proposal=GoalProposal(
                    GoalMutationKind.UPDATE, outstanding_work=(),
                ),
            ),
            AssertionError("the turn continued past the truthful response"),
            selects="goal-1",
        )
        outcome = CoreAgent(
            self.store, reasoner, dispatch, (effectful,), clock=lambda: NOW,
        ).process(conversation(), RETENTION, 8)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "Both items are done.")
        self.assertEqual(dispatched, ["call-1", "call-2"])
        self.assertEqual(len(reasoner.contexts), 4)
        notice = reasoner.contexts[2].continuation_notices[0]
        self.assertEqual(notice["reason"], "remaining_work_still_executable")
        self.assertEqual(notice["outstanding_work"], ["item-2"])
        self.assertEqual(reasoner.contexts[3].continuation_notices, ())
        stored = self.store.load("goal-1").state
        self.assertIs(stored.status, GoalStatus.ACTIVE)
        self.assertEqual(stored.outstanding_work, ())

    def test_a_second_call_less_end_after_the_notice_parks_the_goal(self) -> None:
        """The continuation notice is one-shot; ending again parks remaining work."""
        effectful = CapabilityDefinition(
            "change", "Change structured material", SCHEMA, SCHEMA,
            SideEffect.EFFECTFUL,
        )
        self.store.create(
            goal(outstanding_work=(
                WorkItem("item-1", "first item"),
                WorkItem("item-2", "second item"),
            )),
            "conversation-1",
            RETENTION,
        )
        arguments = {"item_id": "item-1"}
        call = CapabilityCall("call-1", "change", arguments, "approval-1")
        dispatched: list[str] = []

        def dispatch(proposed, state):
            dispatched.append(proposed.call_id)
            return CapabilityAttempt(
                proposed, CapabilityAttemptDisposition.EXECUTED, True,
                CapabilityResult(
                    proposed.call_id, proposed.capability_id,
                    CapabilityResultState.SUCCEEDED, {"ok": True},
                ),
            )

        reasoner = Queued(
            AgentDecision(
                call=call,
                approval_proposal=ApprovalProposal(
                    "approval-1",
                    ApprovalScope("change", arguments),
                    "turn:turn-1",
                ),
            ),
            AgentDecision(response="Still working."),
            AgentDecision(response="I cannot continue from here."),
            AssertionError("a second call-less end continued the loop"),
            selects="goal-1",
        )
        outcome = CoreAgent(
            self.store, reasoner, dispatch, (effectful,), clock=lambda: NOW,
        ).process(conversation(), RETENTION, 8)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "I cannot continue from here.")
        self.assertEqual(dispatched, ["call-1"])
        self.assertEqual(len(reasoner.contexts), 3)
        self.assertEqual(
            reasoner.contexts[2].continuation_notices[0]["reason"],
            "remaining_work_still_executable",
        )
        stored = self.store.load("goal-1").state
        self.assertIs(stored.status, GoalStatus.AWAITING_INPUT)
        self.assertTrue(stored.outstanding_work)

    def test_turn_bound_remaining_work_parks_instead_of_staying_active(self) -> None:
        """Spent turn-bound work cannot continue without another person turn."""
        bound = CapabilityDefinition(
            "review_once", "Request one review", SCHEMA, SCHEMA,
            SideEffect.EFFECTFUL,
        )
        self.store.create(
            goal(outstanding_work=(
                WorkItem("item-1", "first review"),
                WorkItem("item-2", "second review"),
            )),
            "conversation-1",
            RETENTION,
        )
        arguments = {"target": "one"}
        call = CapabilityCall("call-1", "review_once", arguments, "approval-1")
        dispatched: list[str] = []

        def dispatch(proposed, state):
            dispatched.append(proposed.call_id)
            return CapabilityAttempt(
                proposed, CapabilityAttemptDisposition.EXECUTED, True,
                CapabilityResult(
                    proposed.call_id, proposed.capability_id,
                    CapabilityResultState.SUCCEEDED, {"ok": True},
                ),
            )

        reasoner = Queued(
            AgentDecision(
                call=call,
                approval_proposal=ApprovalProposal(
                    "approval-1",
                    ApprovalScope("review_once", arguments),
                    "turn:turn-1",
                ),
            ),
            AgentDecision(
                response="Still working on the rest.",
                goal_proposal=GoalProposal(
                    GoalMutationKind.UPDATE,
                    outstanding_work=(WorkItem("item-2", "second review"),),
                ),
            ),
            AssertionError("a spent turn-bound action continued without a new turn"),
            selects="goal-1",
        )
        outcome = CoreAgent(
            self.store, reasoner, dispatch, (bound,), clock=lambda: NOW,
            turn_bound_capabilities=frozenset({"review_once"}),
        ).process(conversation(), RETENTION, 8)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertNotEqual(outcome.state, CoreState.CHECKPOINTED)
        self.assertEqual(dispatched, ["call-1"])
        self.assertEqual(len(reasoner.contexts), 2)
        stored = self.store.load("goal-1").state
        self.assertIs(stored.status, GoalStatus.AWAITING_INPUT)
        self.assertIs(stored.stop_reason, GoalStopReason.REQUIRED_INPUT)
        self.assertEqual(
            [item.item_id for item in stored.outstanding_work], ["item-2"],
        )

    def test_empty_outstanding_work_ordinary_response_is_unchanged(self) -> None:
        self.store.create(goal(), "conversation-1", RETENTION)
        reasoner = Queued(
            AgentDecision(response="A normal answer."),
            AssertionError("ordinary response continued the loop"),
            selects="goal-1",
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 8)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "A normal answer.")
        self.assertEqual(len(reasoner.contexts), 1)
        self.assertEqual(reasoner.contexts[0].continuation_notices, ())
        stored = self.store.load("goal-1").state
        self.assertIs(stored.status, GoalStatus.ACTIVE)
        self.assertEqual(stored.outstanding_work, ())


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


class AttemptEvidenceCitationTests(unittest.TestCase):
    """Terminal attempts with a result are citable; pending and missing are not."""

    def _call(self, call_id: str = "call-1") -> CapabilityCall:
        return CapabilityCall(call_id, "inspect", {})

    def _succeeded(self, call_id: str = "call-1") -> CapabilityAttempt:
        call = self._call(call_id)
        return CapabilityAttempt(
            call,
            CapabilityAttemptDisposition.EXECUTED,
            True,
            CapabilityResult(call_id, "inspect", CapabilityResultState.SUCCEEDED, {"ok": True}),
        )

    def _failed(self, call_id: str = "call-1") -> CapabilityAttempt:
        call = self._call(call_id)
        return CapabilityAttempt(
            call,
            CapabilityAttemptDisposition.EXECUTED,
            True,
            CapabilityResult(
                call_id,
                "inspect",
                CapabilityResultState.FAILED,
                {"files_changed": ["app.py"]},
                {"code": "task_failed"},
            ),
        )

    def _pending(self, call_id: str = "call-1") -> CapabilityAttempt:
        return CapabilityAttempt(
            self._call(call_id),
            CapabilityAttemptDisposition.PENDING,
            None,
            reason_code="dispatch_pending",
        )

    def _error(self, attempts: tuple, source: str) -> str | None:
        evidence = Evidence(
            "ev-1",
            "observation",
            supports=("criterion-1",),
            source_references=(source,),
        )
        return CoreAgent._evidence_grounding_error(
            conversation(),
            goal(attempts=attempts),
            (),
            (evidence,),
        )

    def test_succeeded_attempt_with_result_is_citable(self) -> None:
        self.assertIsNone(self._error((self._succeeded(),), "attempt:call-1"))

    def test_failed_attempt_with_result_is_citable(self) -> None:
        self.assertIsNone(self._error((self._failed(),), "attempt:call-1"))

    def test_pending_attempt_is_not_citable(self) -> None:
        self.assertEqual(
            self._error((self._pending(),), "attempt:call-1"),
            "evidence_source_unknown",
        )

    def test_nonexistent_attempt_is_not_citable(self) -> None:
        self.assertEqual(
            self._error((self._failed(),), "attempt:call-missing"),
            "evidence_source_unknown",
        )

    def test_failed_attempt_can_be_recorded_without_completing_the_goal(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = SQLiteGoalStore(Path(directory.name) / "goals.sqlite3")
        self.addCleanup(store.close)
        store.create(goal(), "conversation-1", RETENTION)
        call = self._call()
        failed = self._failed()
        evidence = Evidence(
            "ev-coding",
            "coding_job",
            supports=("criterion-1",),
            source_references=("attempt:call-1",),
        )
        reasoner = Queued(
            AgentDecision(call=call),
            AgentDecision(
                response="The job ran and failed its tests.",
                goal_proposal=GoalProposal(
                    GoalMutationKind.UPDATE,
                    new_evidence=(evidence,),
                ),
            ),
            selects="goal-1",
        )
        outcome = CoreAgent(
            store, reasoner, lambda proposed, state: failed, (DEFINITION,),
            clock=lambda: NOW,
        ).process(conversation(), RETENTION, 5)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertNotEqual(outcome.reason, "goal_proposal_rejected")
        stored = store.load("goal-1").state
        self.assertEqual(stored.status, GoalStatus.ACTIVE)
        self.assertEqual(stored.evidence, (evidence,))
        self.assertIs(stored.attempts[0].result.state, CapabilityResultState.FAILED)

    def test_mutation_succeeded_only_rule_reproduces_the_live_rejection(self) -> None:
        source = inspect.getsource(CoreAgent._attempt_is_citable_evidence_source)
        self.assertIn("CapabilityResultState.FAILED", source)
        mutated = source.replace(
            "        return result.state in {\n"
            "            CapabilityResultState.SUCCEEDED,\n"
            "            CapabilityResultState.FAILED,\n"
            "        }",
            "        return result.state is CapabilityResultState.SUCCEEDED",
        )
        self.assertNotEqual(source, mutated)
        original = CoreAgent._attempt_is_citable_evidence_source

        def succeeded_only(item):
            return (
                original(item)
                and item.result is not None
                and item.result.state is CapabilityResultState.SUCCEEDED
            )

        CoreAgent._attempt_is_citable_evidence_source = staticmethod(succeeded_only)
        try:
            self.assertEqual(
                self._error((self._failed(),), "attempt:call-1"),
                "evidence_source_unknown",
            )
        finally:
            CoreAgent._attempt_is_citable_evidence_source = original
        self.assertIsNone(self._error((self._failed(),), "attempt:call-1"))


if __name__ == "__main__":
    unittest.main()
