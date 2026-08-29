"""Regression evidence for the review findings on the mail slice.

Each test corresponds to a defect that was reproduced against the code before
being fixed. Three came from the Greptile review of PR #8; the approval-release
defect was found in Friedl's own durable goal state, where one request produced
five consecutive rejected Trash attempts.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.capabilities import CapabilityBroker, CapabilityRegistry  # noqa: E402
from alx.contracts import (  # noqa: E402
    AgentDecision, ApprovalLifecycle, ApprovalProposal, ApprovalScope,
    BackgroundEvent, CapabilityAttemptDisposition, CapabilityCall,
    CapabilityDefinition, CapabilityResult, CapabilityResultState,
    ConversationOrigin, ConversationSnapshot, ConversationTurn, Evidence,
    GoalMutationKind, GoalProposal, MemoryKind, MemoryProposal, SideEffect,
    StructuredSchema, SuccessCriterion, ValueKind,
)
from alx.core import CoreAgent, CoreState  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402
from alx.safety import AuthorityContext, AuthorityPolicy, SafetyGate  # noqa: E402

NOW = datetime(2026, 8, 29, tzinfo=UTC)
RETENTION = NOW + timedelta(days=30)
SCHEMA = StructuredSchema(ValueKind.OBJECT)
TRASH = "move_mail_message_to_trash"
ARGS = {"mailbox_id": "INBOX", "uid": "58603", "uid_validity": "1376545928"}
DEFINITION = CapabilityDefinition(TRASH, "move one message to trash", SCHEMA, SCHEMA,
                                  SideEffect.EFFECTFUL)


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


def conversation(*contents: str, events=()) -> ConversationSnapshot:
    turns = tuple(
        ConversationTurn("conversation-1", f"turn-{i + 1}", ConversationOrigin.TYPED,
                         text, NOW, "friedl")
        for i, text in enumerate(contents or ("trash that reminder",))
    )
    return ConversationSnapshot("conversation-1", turns, len(turns), RETENTION, events)


class ApprovalReleaseTests(unittest.TestCase):
    """A refused or impossible action must not burn Friedl's approval."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = SQLiteGoalStore(Path(self.directory.name) / "goals.sqlite3")
        self.invocations: list[object] = []

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def broker_for(self, executor):
        return CapabilityBroker(
            CapabilityRegistry((DEFINITION,)),
            SafetyGate({TRASH: AuthorityPolicy(frozenset({"mail.trash"}),
                                               approval_required=True)}),
            {TRASH: executor},
        )

    def dispatcher(self, broker):
        def dispatch(call, state):
            return broker.dispatch(
                call, AuthorityContext("friedl", frozenset({"mail.trash"}), NOW,
                                       state.approvals)
            )
        return dispatch

    def first_turn(self, executor):
        broker = self.broker_for(executor)
        reasoner = Queued(
            AgentDecision(
                goal_proposal=GoalProposal(
                    kind=GoalMutationKind.CREATE,
                    objective_summary="Move the reminder to Trash",
                    success_criteria=(SuccessCriterion("criterion-1", "moved"),)),
                call=CapabilityCall("call-1", TRASH, ARGS, "approval-1"),
                approval_proposal=ApprovalProposal(
                    "approval-1", ApprovalScope(TRASH, ARGS), "turn:turn-1"),
            ),
            AgentDecision(response="That did not go through."),
        )
        agent = CoreAgent(self.store, reasoner, self.dispatcher(broker), (DEFINITION,),
                          clock=lambda: NOW, identifier_factory=lambda: "goal-1")
        return agent.process(conversation(), None, RETENTION, 4)

    def test_a_failed_move_returns_the_approval_for_a_retry(self) -> None:
        """The defect that produced five rejected Trash attempts in real state."""
        def explode(arguments):
            raise RuntimeError("imap blip")

        self.first_turn(explode)
        approvals = self.store.load("goal-1").state.approvals
        self.assertEqual(len(approvals), 1)
        self.assertIs(approvals[0].lifecycle, ApprovalLifecycle.GRANTED,
                      "a definite pre-effect failure must not spend the approval")

    def test_the_released_approval_actually_authorises_the_retry(self) -> None:
        attempts = {"count": 0}

        def flaky(arguments):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("imap blip")
            return CapabilityResult("call-2", TRASH, CapabilityResultState.SUCCEEDED, {})

        self.first_turn(flaky)
        broker = self.broker_for(flaky)
        reasoner = Queued(
            AgentDecision(call=CapabilityCall("call-2", TRASH, ARGS, "approval-1")),
            AgentDecision(response="Moved to Trash."),
        )
        agent = CoreAgent(self.store, reasoner, self.dispatcher(broker), (DEFINITION,),
                          clock=lambda: NOW, identifier_factory=lambda: "goal-1")
        outcome = agent.process(conversation("trash it", "try again"), "goal-1",
                                RETENTION, 4)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(attempts["count"], 2, "the retry must reach the executor")
        approvals = self.store.load("goal-1").state.approvals
        self.assertIs(approvals[0].lifecycle, ApprovalLifecycle.CONSUMED)

    def test_an_action_that_may_have_taken_effect_keeps_the_approval_spent(self) -> None:
        """An ambiguous outcome must never be retried automatically."""
        def malformed(arguments):
            return "not a capability result"

        self.first_turn(malformed)
        approvals = self.store.load("goal-1").state.approvals
        self.assertIs(approvals[0].lifecycle, ApprovalLifecycle.CONSUMED)

    def test_a_refused_action_is_not_retried_under_a_fresh_identifier(self) -> None:
        """A new call or approval id does not make a refused action different."""
        dispatched: list[CapabilityCall] = []

        def dispatch(call, state):
            dispatched.append(call)
            from alx.contracts import CapabilityAttempt
            return CapabilityAttempt(call, CapabilityAttemptDisposition.REJECTED,
                                     False, reason_code="approval_invalid")

        reasoner = Queued(
            AgentDecision(
                goal_proposal=GoalProposal(
                    kind=GoalMutationKind.CREATE,
                    objective_summary="Move the reminder to Trash",
                    success_criteria=(SuccessCriterion("criterion-1", "moved"),)),
                call=CapabilityCall("call-1", TRASH, ARGS, "approval-1")),
            AgentDecision(call=CapabilityCall("call-2", TRASH, ARGS, "approval-2")),
            AgentDecision(response="unreachable"),
        )
        agent = CoreAgent(self.store, reasoner, dispatch, (DEFINITION,),
                          clock=lambda: NOW, identifier_factory=lambda: "goal-1")
        outcome = agent.process(conversation(), None, RETENTION, 4)
        self.assertEqual(outcome.reason, "repeated_rejected_call")
        self.assertEqual(len(dispatched), 1, "the refused action must not repeat")


class TransientContentTests(unittest.TestCase):
    """A mail body shown to the model must not reach durable state."""

    BODY = (
        "Thanks for waiting. Revision B is quoted at R14 500 for 25 units, with a "
        "ten working day lead time, and we can start production once you confirm "
        "the order in writing."
    )

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = SQLiteGoalStore(Path(self.directory.name) / "goals.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def conversation_with_body(self) -> ConversationSnapshot:
        event = BackgroundEvent(
            "event-1", "mail.observed", NOW,
            data={"uid": "58603", "subject": "Re: Panel revision B quote"},
            transient_data={"body_text": self.BODY},
        )
        return conversation("what does that one say?", events=(event,))

    def agent(self, reasoner) -> CoreAgent:
        return CoreAgent(self.store, reasoner, lambda call, state: None, (DEFINITION,),
                         clock=lambda: NOW, identifier_factory=lambda: "goal-1")

    def test_evidence_quoting_the_body_is_refused(self) -> None:
        reasoner = Queued(AgentDecision(
            goal_proposal=GoalProposal(
                kind=GoalMutationKind.CREATE,
                objective_summary="Answer about the quote",
                success_criteria=(SuccessCriterion("criterion-1", "answered"),),
                new_evidence=(Evidence("evidence-1", "mail",
                                       attributes={"quoted": self.BODY},
                                       supports=("criterion-1",),
                                       source_references=("event:event-1",)),)),
            response="Here is what it says.",
            response_requires_goal_commit=True,
        ))
        outcome = self.agent(reasoner).process(
            self.conversation_with_body(), None, RETENTION, 2)
        self.assertEqual(outcome.state, CoreState.ERROR)
        self.assertEqual(outcome.reason, "goal_proposal_invalid")

    def test_a_memory_quoting_the_body_is_refused(self) -> None:
        reasoner = Queued(AgentDecision(
            response="Noted.",
            memory_proposals=(MemoryProposal(
                "memory-1", MemoryKind.FACTUAL, self.BODY,
                ("event:event-1",), NOW),),
        ))
        outcome = self.agent(reasoner).process(
            self.conversation_with_body(), None, RETENTION, 2)
        self.assertEqual(outcome.state, CoreState.ERROR)
        self.assertEqual(outcome.reason, "memory_proposal_invalid")

    def test_the_core_may_still_describe_the_message_in_its_own_words(self) -> None:
        """The guard must not block legitimate summarisation."""
        reasoner = Queued(AgentDecision(
            goal_proposal=GoalProposal(
                kind=GoalMutationKind.CREATE,
                objective_summary="Answer about the quote",
                success_criteria=(SuccessCriterion("criterion-1", "answered"),),
                new_evidence=(Evidence("evidence-1", "mail",
                                       attributes={"gist": "The board house quoted revision B."},
                                       supports=("criterion-1",),
                                       source_references=("event:event-1",)),)),
            response="They quoted revision B with a ten day lead time.",
            response_requires_goal_commit=True,
        ))
        outcome = self.agent(reasoner).process(
            self.conversation_with_body(), None, RETENTION, 2)
        self.assertEqual(outcome.state, CoreState.RESPONDED)

    def test_durable_event_data_remains_usable(self) -> None:
        """Only transient content is guarded; durable event data is not."""
        reasoner = Queued(AgentDecision(
            goal_proposal=GoalProposal(
                kind=GoalMutationKind.CREATE,
                objective_summary="Track the message",
                success_criteria=(SuccessCriterion("criterion-1", "tracked"),),
                new_evidence=(Evidence("evidence-1", "mail",
                                       attributes={"subject": "Re: Panel revision B quote"},
                                       supports=("criterion-1",),
                                       source_references=("event:event-1",)),)),
            response="Tracked.",
            response_requires_goal_commit=True,
        ))
        outcome = self.agent(reasoner).process(
            self.conversation_with_body(), None, RETENTION, 2)
        self.assertEqual(outcome.state, CoreState.RESPONDED)


class TrashAuthorisationTests(unittest.TestCase):
    """D-010 records the authorisation; these assert the safeguards it claims."""

    def test_trash_requires_its_own_permission_and_an_approval(self) -> None:
        from alx.bootstrap.mail import (
            MAIL_READ_PERMISSION, MAIL_TRASH_PERMISSION,
        )

        self.assertNotEqual(MAIL_TRASH_PERMISSION, MAIL_READ_PERMISSION)

    def test_only_trash_carries_external_side_effects(self) -> None:
        from alx.tools import DEFINITIONS

        effectful = [item.capability_id for item in DEFINITIONS
                     if item.side_effect is SideEffect.EFFECTFUL]
        self.assertEqual(effectful, [TRASH])

    def test_no_permanent_deletion_is_reachable(self) -> None:
        source = (Path(__file__).resolve().parents[1]
                  / "src/alx/providers/icloud_mail.py").read_text("utf-8")
        for forbidden in ("EXPUNGE", "\\\\Deleted"):
            self.assertNotIn(forbidden, source)

    def test_the_authorisation_is_recorded(self) -> None:
        decisions = (Path(__file__).resolve().parents[1]
                     / "governance/DECISIONS.md").read_text("utf-8")
        self.assertIn("D-010", decisions)
        self.assertIn(TRASH, decisions)


if __name__ == "__main__":
    unittest.main()
