from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    AgentDecision, CapabilityAttempt, CapabilityAttemptDisposition,
    CapabilityCall, CapabilityDefinition, CapabilityResult,
    CapabilityResultState, ConversationOrigin, ConversationSnapshot,
    ConversationTurn, DecisionValidationError, Evidence, GoalMutationKind,
    GoalProposal, GoalState, GoalStatus, Objective, SideEffect,
    MemoryKind, MemoryProposal, StructuredSchema, SuccessCriterion, ValueKind,
    WorkItem,
)
from alx.core import CoreAgent, CoreState  # noqa: E402
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
    def __init__(self, *decisions) -> None:
        self.decisions = list(decisions)
        self.contexts = []

    def decide(self, context):
        self.contexts.append(context)
        item = self.decisions.pop(0)
        if isinstance(item, Exception):
            raise item
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
        outcome = self.agent(reasoner).process(conversation(), None, RETENTION, 1)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "A normal answer.")
        self.assertIsNone(outcome.snapshot)
        self.assertIsNone(reasoner.contexts[0].active_goal)
        self.assertEqual(self.store.list_goals(), ())

    def test_core_creates_goal_only_from_optional_proposal(self) -> None:
        proposal = GoalProposal(
            GoalMutationKind.CREATE,
            "Investigate the fault",
            (SuccessCriterion("criterion-1", "cause verified"),),
        )
        outcome = self.agent(Queued(AgentDecision(response="I’ll investigate.",
                                                   goal_proposal=proposal))).process(
            conversation(), None, RETENTION, 1,
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
                                        goal_proposal=proposal))
        outcome = self.agent(reasoner).process(conversation(), "goal-1", RETENTION, 2)
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
        )
        outcome = self.agent(reasoner).process(conversation(), "goal-1", RETENTION, 2)
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
        ))).process(conversation(), "goal-1", RETENTION, 1)
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
                                                   response_requires_goal_commit=True))).process(
            conversation(), "goal-1", RETENTION, 1,
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.snapshot.state.status, GoalStatus.COMPLETED)
        self.assertEqual(outcome.snapshot.state.evidence, (evidence,))

    def test_completion_rejects_outstanding_work_even_with_evidence(self) -> None:
        self.store.create(goal(outstanding_work=(WorkItem("work-1", "verify"),)),
                          "conversation-1", RETENTION)
        evidence = Evidence("evidence-1", "fact", supports=("criterion-1",),
                            source_references=("turn:turn-1",))
        proposal = GoalProposal(GoalMutationKind.REQUEST_COMPLETION,
                                new_evidence=(evidence,))
        outcome = self.agent(Queued(AgentDecision(response="Still working.",
                                                   goal_proposal=proposal))).process(
            conversation(), "goal-1", RETENTION, 1,
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
        )
        outcome = self.agent(reasoner, lambda proposed, state: attempt).process(
            conversation(), "goal-1", RETENTION, 2,
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(reasoner.contexts[1].active_goal.attempts, (attempt,))
        self.assertEqual(outcome.snapshot.state.status, GoalStatus.ACTIVE)

    def test_provider_validation_error_is_not_retried(self) -> None:
        reasoner = Queued(DecisionValidationError("malformed"),
                          AssertionError("retry occurred"))
        outcome = self.agent(reasoner).process(conversation(), None, RETENTION, 3)
        self.assertEqual(outcome.reason, "reasoner_error")
        self.assertEqual(len(reasoner.contexts), 1)

    def test_unresolved_dispatch_survives_restart_and_blocks_repeat(self) -> None:
        self.store.create(goal(), "conversation-1", RETENTION)
        call = CapabilityCall("call-1", "inspect", {})
        outcome = self.agent(
            Queued(AgentDecision(call=call)),
            lambda proposed, state: (_ for _ in ()).throw(RuntimeError()),
        ).process(conversation(), "goal-1", RETENTION, 1)
        self.assertEqual(outcome.reason, "dispatch_error")
        self.store.close()
        self.store = SQLiteGoalStore(self.path)
        reasoner = Queued(AssertionError("pending action reached model"))
        resumed = self.agent(reasoner).process(conversation(), "goal-1", RETENTION, 1)
        self.assertEqual(resumed.reason, "dispatch_unresolved")
        self.assertEqual(reasoner.contexts, [])


if __name__ == "__main__":
    unittest.main()
