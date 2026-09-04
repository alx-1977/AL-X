"""Two live defects from the 2026-09-04 commissioning session.

Defect 1B -- a refused effectful call ends the turn silently.
    Friedl asked AL/X to delete an email. Her goal proposal was rejected for a
    provenance reason, so no goal was active, so the dispatch was correctly
    blocked -- and the turn ended with `response=False`. He was told nothing.
    Blocking the dispatch is right. Saying nothing is not.

Defect 2 -- mail delivery bookkeeping kills the live transport.
    After AL/X had already answered a background mail event, the runtime
    called `record_delivery()`. The observation had meanwhile reached `done`,
    the UPDATE matched no rows, `MailAccessError` was raised, and it escaped
    to the transport and killed the browser session. Twice.

The grounding contract itself is NOT broken: `event:mail:<validity>:<uid>` is
offered to the Core and accepted by goal grounding. `MailGroundingTest` proves
that, so a future change cannot quietly break it.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    AgentDecision, BackgroundEvent, CapabilityCall, CapabilityDefinition,
    ConversationOrigin, ConversationSnapshot, ConversationTurn, Evidence,
    GoalMutationKind, GoalProposal, MailAccessError, MailReference,
    SideEffect, StructuredSchema, SuccessCriterion, ValueKind,
)
from alx.core import CoreAgent, CoreState  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402
from alx.providers.icloud_mail import SQLiteMailObservationState  # noqa: E402

NOW = datetime(2026, 9, 4, 6, 0, tzinfo=UTC)
RETENTION = NOW + timedelta(days=30)
SCHEMA = StructuredSchema(ValueKind.OBJECT)
TRASH = CapabilityDefinition(
    "move_mail_message_to_trash", "Move one message to Trash",
    SCHEMA, SCHEMA, SideEffect.EFFECTFUL,
)
MAIL_EVENT_ID = "mail:777:3"


def conversation() -> ConversationSnapshot:
    turn = ConversationTurn(
        "c1", "t1", ConversationOrigin.TYPED,
        "please delete the email from Quinton", NOW, "friedl",
    )
    event = BackgroundEvent(
        MAIL_EVENT_ID, "mail.message_arrived", NOW, {"uid": "3"},
    )
    return ConversationSnapshot("c1", (turn,), 1, RETENTION, events=(event,))


def goal_proposal(source: str) -> GoalProposal:
    return GoalProposal(
        GoalMutationKind.CREATE,
        objective_summary="Delete the message from Quinton",
        success_criteria=(SuccessCriterion("c1", "message is in Trash"),),
        new_evidence=(Evidence("e1", "request", {}, ("c1",), (source,)),),
    )


class Queued:
    def __init__(self, *decisions: AgentDecision) -> None:
        self.decisions = list(decisions)
        self.contexts: list = []

    def decide(self, context):
        self.contexts.append(context)
        return self.decisions.pop(0)


def observation_store(directory: str) -> SQLiteMailObservationState:
    """A real store holding one observation, moved to `current`."""
    state = SQLiteMailObservationState(Path(directory) / "mail.sqlite3")
    state.new_identifiers("INBOX", "777", ())
    state.discover("INBOX", "777", ((3, {
        "mailbox_id": "INBOX", "uid_validity": "777", "uid": "3",
        "observed_at": "2026-09-04T06:00:00+00:00", "subject": "From Quinton",
    }),), (3,))
    return state


class MailGroundingTest(unittest.TestCase):
    """A mail event can ground a goal, through the ordinary `event:` form."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SQLiteGoalStore(Path(self.directory.name) / "goals.sqlite3")

    def outcome(self, source: str):
        reasoner = Queued(AgentDecision(
            goal_proposal=goal_proposal(source), response="Working on it."))
        return CoreAgent(
            self.store, reasoner, lambda call, state: None, (TRASH,),
            clock=lambda: NOW,
        ).process(conversation(), RETENTION, 2)

    def test_a_mail_event_grounds_a_goal(self) -> None:
        """`event:mail:<validity>:<uid>` is the supplied durable reference."""
        outcome = self.outcome(f"event:{MAIL_EVENT_ID}")
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertIsNone(outcome.reason, "the goal must be accepted")

    def test_the_person_turn_also_grounds_a_goal(self) -> None:
        outcome = self.outcome("turn:t1")
        self.assertIsNone(outcome.reason)

    def test_a_bare_mail_id_is_refused(self) -> None:
        """No `mail:` grounding form exists, and none is being added."""
        self.assertEqual(
            self.outcome(MAIL_EVENT_ID).reason, "goal_proposal_rejected",
        )

    def test_an_unknown_event_is_refused(self) -> None:
        self.assertEqual(
            self.outcome("event:mail:777:99").reason, "goal_proposal_rejected",
        )

    def test_no_mail_specific_routing_decides_any_of_this(self) -> None:
        """Grounding is provenance validation only -- it never reads meaning.

        The same capability, the same evidence shape and an unrelated
        objective ground identically: nothing inspects what the words say.
        """
        reasoner = Queued(AgentDecision(
            goal_proposal=GoalProposal(
                GoalMutationKind.CREATE,
                objective_summary="Something entirely unrelated to mail",
                success_criteria=(SuccessCriterion("c1", "done"),),
                new_evidence=(
                    Evidence("e1", "x", {}, ("c1",), (f"event:{MAIL_EVENT_ID}",)),
                ),
            ),
            response="Fine."))
        outcome = CoreAgent(
            self.store, reasoner, lambda call, state: None, (TRASH,),
            clock=lambda: NOW,
        ).process(conversation(), RETENTION, 2)
        self.assertIsNone(outcome.reason)


class BlockedDispatchTest(unittest.TestCase):
    """A refused effectful call must not end the turn in silence."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SQLiteGoalStore(Path(self.directory.name) / "goals.sqlite3")
        self.dispatched: list = []

    def run_turn(self, *decisions: AgentDecision, budget: int = 3):
        self.reasoner = Queued(*decisions)
        return CoreAgent(
            self.store, self.reasoner,
            lambda call, state: self.dispatched.append(call), (TRASH,),
            clock=lambda: NOW,
        ).process(conversation(), RETENTION, budget)

    def test_the_refusal_returns_to_the_core_with_the_reason(self) -> None:
        outcome = self.run_turn(
            AgentDecision(
                goal_proposal=goal_proposal(MAIL_EVENT_ID),   # bad prefix
                call=CapabilityCall("call-1", "move_mail_message_to_trash", {}),
            ),
            AgentDecision(response="I cannot ground that safely yet."),
        )
        self.assertGreaterEqual(
            len(self.reasoner.contexts), 2,
            "she must be told why the dispatch was impossible",
        )
        refusals = self.reasoner.contexts[1].refused_calls
        self.assertEqual(len(refusals), 1)
        self.assertEqual(refusals[0]["reason"], "active_goal_required")
        self.assertEqual(
            refusals[0]["capability_id"], "move_mail_message_to_trash",
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "I cannot ground that safely yet.")

    def test_nothing_is_dispatched_behind_the_refusal(self) -> None:
        self.run_turn(
            AgentDecision(
                goal_proposal=goal_proposal(MAIL_EVENT_ID),
                call=CapabilityCall("call-1", "move_mail_message_to_trash", {}),
            ),
            AgentDecision(response="I cannot do that yet."),
        )
        self.assertEqual(self.dispatched, [])
        self.assertEqual(self.store.list_goals(), ())

    def test_an_unexplained_refusal_cannot_loop(self) -> None:
        """The reason is given once. Repeating the same call ends the turn.

        The original design stopped immediately because reasoning again from
        an unchanged state bought paid calls for nothing. Telling her the
        reason changes the state exactly once; insisting after that does not.
        The allowance is one per turn, not one per capability, so cycling
        through different impossible capabilities cannot spend the budget.
        """
        outcome = self.run_turn(
            AgentDecision(
                goal_proposal=goal_proposal(MAIL_EVENT_ID),
                call=CapabilityCall("call-1", "move_mail_message_to_trash", {}),
            ),
            AgentDecision(
                goal_proposal=goal_proposal(MAIL_EVENT_ID),
                call=CapabilityCall("call-2", "move_mail_message_to_trash", {}),
            ),
            AgentDecision(response="unreachable"),
            budget=6,
        )
        self.assertEqual(outcome.state, CoreState.CHECKPOINTED)
        self.assertEqual(outcome.reason, "active_goal_required")
        self.assertEqual(len(self.reasoner.contexts), 2)
        self.assertEqual(self.dispatched, [])

    def test_a_grounded_goal_still_dispatches_normally(self) -> None:
        """The refusal path must not disturb the ordinary effectful route."""
        from alx.contracts import (
            ApprovalProposal, ApprovalScope, CapabilityAttempt,
            CapabilityAttemptDisposition, CapabilityResult,
            CapabilityResultState,
        )
        call = CapabilityCall(
            "call-1", "move_mail_message_to_trash", {}, "approval-1")
        dispatched: list = []

        def dispatch(proposed, state):
            # The attempt must echo the call the loop actually issued.
            dispatched.append(proposed)
            return CapabilityAttempt(
                proposed, CapabilityAttemptDisposition.EXECUTED, True,
                CapabilityResult(
                    proposed.call_id, proposed.capability_id,
                    CapabilityResultState.SUCCEEDED, {"moved": True}),
            )

        reasoner = Queued(
            AgentDecision(
                goal_proposal=goal_proposal(f"event:{MAIL_EVENT_ID}"),
                call=call,
                approval_proposal=ApprovalProposal(
                    "approval-1",
                    ApprovalScope("move_mail_message_to_trash", {}),
                    "turn:t1",
                ),
            ),
            AgentDecision(response="Done."),
        )
        outcome = CoreAgent(
            self.store, reasoner, dispatch, (TRASH,), clock=lambda: NOW,
        ).process(conversation(), RETENTION, 3)
        self.assertEqual(outcome.state, CoreState.RESPONDED, outcome.reason)
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(
            dispatched[0].capability_id, "move_mail_message_to_trash",
        )
        self.assertEqual(reasoner.contexts[1].refused_calls, ())


class RejectedProposalObservabilityTest(unittest.TestCase):
    """The rejected proposal left no record, so it could not be diagnosed."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SQLiteGoalStore(Path(self.directory.name) / "goals.sqlite3")

    def test_a_rejection_records_its_references_and_reason(self) -> None:
        records: list = []
        reasoner = Queued(
            AgentDecision(goal_proposal=goal_proposal(MAIL_EVENT_ID),
                          response="Working on it."),
        )
        CoreAgent(
            self.store, reasoner, lambda call, state: None, (TRASH,),
            clock=lambda: NOW, record_goal_rejection=records.append,
        ).process(conversation(), RETENTION, 2)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["reason"], "evidence_source_unknown")
        self.assertEqual(record["source_references"], [MAIL_EVENT_ID])
        self.assertEqual(record["conversation_id"], "c1")
        self.assertEqual(record["recorded_at"], NOW.isoformat())

    def test_no_reasoning_or_prose_is_persisted(self) -> None:
        """Mechanical metadata only: no objective text, no model payload."""
        records: list = []
        reasoner = Queued(
            AgentDecision(goal_proposal=goal_proposal(MAIL_EVENT_ID),
                          response="Working on it."),
        )
        CoreAgent(
            self.store, reasoner, lambda call, state: None, (TRASH,),
            clock=lambda: NOW, record_goal_rejection=records.append,
        ).process(conversation(), RETENTION, 2)
        serialised = repr(records[0])
        self.assertNotIn("Delete the message from Quinton", serialised)
        self.assertNotIn("Working on it", serialised)

    def test_an_accepted_proposal_records_nothing(self) -> None:
        records: list = []
        reasoner = Queued(
            AgentDecision(goal_proposal=goal_proposal(f"event:{MAIL_EVENT_ID}"),
                          response="Working on it."),
        )
        CoreAgent(
            self.store, reasoner, lambda call, state: None, (TRASH,),
            clock=lambda: NOW, record_goal_rejection=records.append,
        ).process(conversation(), RETENTION, 2)
        self.assertEqual(records, [])


class DeliveryBookkeepingTest(unittest.TestCase):
    """`record_delivery` against a real store, in each reachable state."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = observation_store(self.directory.name)
        self.addCleanup(self.state.close)

    def test_normal_delivery_recording_still_works(self) -> None:
        event = self.state.current()
        self.assertEqual(event.event_id, MAIL_EVENT_ID)
        self.assertTrue(
            self.state.record_delivery(event.event_id),
            "a current observation records its delivery and reports it",
        )

    def test_an_already_reconciled_observation_is_not_an_error(self) -> None:
        """The live failure: acknowledged in-turn, then recorded after.

        Reported as "nothing to record", not raised. The announcement already
        reached Friedl; the bookkeeping simply has nothing left to do.
        """
        event = self.state.current()
        self.state.acknowledge(MailReference("INBOX", "777", "3"))
        self.assertFalse(
            self.state.record_delivery(event.event_id),
            "an already-reconciled observation is benign, not a failure",
        )

    def test_a_second_recording_is_benign(self) -> None:
        event = self.state.current()
        self.assertTrue(self.state.record_delivery(event.event_id))
        self.assertFalse(
            self.state.record_delivery(event.event_id),
            "recording twice must not raise; it simply changes nothing",
        )

    def test_it_does_not_resurrect_a_done_observation(self) -> None:
        event = self.state.current()
        self.state.acknowledge(MailReference("INBOX", "777", "3"))
        self.state.record_delivery(event.event_id)
        self.assertIsNone(
            self.state.current(),
            "a reconciled observation must not return to the conversation",
        )

    def test_a_malformed_event_id_is_still_an_error(self) -> None:
        for bad in ("nonsense", "mail:777", "mail:777:x", "other:777:3"):
            with self.subTest(event_id=bad):
                with self.assertRaises(MailAccessError):
                    self.state.record_delivery(bad)

    def test_a_genuine_storage_failure_is_still_raised(self) -> None:
        self.state.close()
        with self.assertRaises((MailAccessError, sqlite3.ProgrammingError)):
            self.state.record_delivery(MAIL_EVENT_ID)


if __name__ == "__main__":
    unittest.main()
