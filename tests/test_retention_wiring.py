"""D-013 evidence that provenance survives the authoritative durable path."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alx.contracts import (
    AgentDecision,
    BackgroundEvent,
    ContentOrigin,
    GoalMutationKind,
    GoalProposal,
    GoalState,
    MailReference,
    MemoryKind,
    MemoryProposal,
    Objective,
    RetentionPolicy,
    SuccessCriterion,
)
from alx.conversation import ConversationGateway, SQLiteConversationStore
from alx.core import CoreAgent, CoreState
from alx.goals import SQLiteGoalStore
from alx.goals.store import _goal_to_data
from alx.memories import SQLiteMemoryStore
from alx.safety.retention import Classification
from scripts.inventory_retention import survey


NOW = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
CORE_TIME = NOW + timedelta(hours=1)
CONTAINER_RETENTION = NOW + timedelta(days=3650)
REFERENCE = MailReference("INBOX", "777", "42")


class OneDecision:
    def __init__(self, decision: AgentDecision) -> None:
        self.decision = decision

    def decide(self, context):
        return self.decision


def _goal(identifier: str = "goal-1") -> GoalState:
    return GoalState(
        identifier,
        Objective("event:mail:777:42", "Handle the quote"),
        (SuccessCriterion("criterion-1", "The quote is handled"),),
    )


class AuthoritativePathTests(unittest.TestCase):
    def test_mail_provenance_reaches_goal_memory_and_response_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / ".alx/runtime"
            runtime.mkdir(parents=True)
            conversations_path = runtime / "conversations.sqlite3"
            goals_path = runtime / "goals.sqlite3"
            memories_path = runtime / "memories.sqlite3"
            conversations = SQLiteConversationStore(conversations_path)
            goals = SQLiteGoalStore(goals_path)
            memories = SQLiteMemoryStore(memories_path)
            goal_proposal = GoalProposal(
                GoalMutationKind.CREATE,
                "Handle the quote",
                (SuccessCriterion("criterion-1", "The quote is handled"),),
            )
            memory = MemoryProposal(
                "memory-1",
                MemoryKind.FACTUAL,
                "The supplier sent a quote.",
                ("event:mail:777:42",),
                CORE_TIME,
            )
            reasoner = OneDecision(
                AgentDecision(
                    response="The supplier sent a quote.",
                    goal_proposal=goal_proposal,
                    memory_proposals=(memory,),
                )
            )
            core = CoreAgent(
                goals,
                reasoner,
                lambda call, state: None,
                (),
                memory_store=memories,
                clock=lambda: CORE_TIME,
                identifier_factory=lambda: "goal-1",
            )
            gateway = ConversationGateway(
                core,
                conversations,
                identifier_factory=lambda: "response-1",
                clock=lambda: CORE_TIME,
            )
            source = RetentionPolicy().direct_mail(NOW, (REFERENCE,))
            event = BackgroundEvent(
                "mail:777:42",
                "mail.message_arrived",
                NOW,
                {"mailbox_id": "INBOX", "uid_validity": "777", "uid": "42"},
                {"body": "private quote"},
                source,
            )
            outcome = gateway.receive_background_event(
                "conversation-1", event, 1, CONTAINER_RETENTION
            )
            self.assertEqual(outcome.state, CoreState.RESPONDED)
            conversations.close()
            goals.close()
            memories.close()

            conversations = SQLiteConversationStore(conversations_path)
            goals = SQLiteGoalStore(goals_path)
            memories = SQLiteMemoryStore(memories_path)
            try:
                provenances = (
                    conversations.load("conversation-1").turns[-1].provenance,
                    goals.load("goal-1").provenance,
                    memories.load("memory-1").current.provenance,
                )
                for provenance in provenances:
                    self.assertIsNotNone(provenance)
                    self.assertIn(ContentOrigin.MAIL_MESSAGE, provenance.origins)
                    self.assertEqual(provenance.mail_references, (REFERENCE,))
                    self.assertEqual(
                        provenance.content_expires_at, NOW + timedelta(days=30)
                    )
                records = survey(runtime)
                self.assertEqual(len(records), 3)
                self.assertTrue(
                    all(
                        item.classification is Classification.MAIL_DERIVED
                        for item in records
                    )
                )
            finally:
                conversations.close()
                goals.close()
                memories.close()


class StoreInvariantTests(unittest.TestCase):
    def test_goal_store_preserves_provenance_and_rejects_renewal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteGoalStore(Path(directory) / "goals.sqlite3")
            original = RetentionPolicy().direct_mail(NOW, (REFERENCE,))
            try:
                saved = store.create(
                    _goal(), "conversation-1", CONTAINER_RETENTION, original
                )
                preserved = store.replace(
                    saved.state,
                    saved.retention_until,
                    saved.revision,
                )
                self.assertEqual(preserved.provenance, original)
                renewed = RetentionPolicy().direct_mail(
                    NOW + timedelta(days=1), (REFERENCE,)
                )
                with self.assertRaises(ValueError):
                    store.replace(
                        preserved.state,
                        preserved.retention_until,
                        preserved.revision,
                        renewed,
                    )
                stripped = RetentionPolicy().non_mail(ContentOrigin.ALX, CORE_TIME)
                with self.assertRaises(ValueError):
                    store.replace(
                        preserved.state,
                        preserved.retention_until,
                        preserved.revision,
                        stripped,
                    )
            finally:
                store.close()

    def test_v4_goal_rows_migrate_as_unclassified_without_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "goals.sqlite3"
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "CREATE TABLE goals (goal_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, retention_until TEXT NOT NULL, state_json TEXT NOT NULL, conversation_id TEXT)"
                )
                connection.execute(
                    "CREATE TABLE conversation_turns (goal_id TEXT NOT NULL REFERENCES goals(goal_id) ON DELETE CASCADE, ordinal INTEGER NOT NULL, turn_id TEXT NOT NULL, turn_json TEXT NOT NULL, PRIMARY KEY(goal_id, ordinal), UNIQUE(goal_id, turn_id))"
                )
                connection.execute(
                    "CREATE TABLE pending_memory_batches (goal_id TEXT NOT NULL REFERENCES goals(goal_id) ON DELETE CASCADE, goal_revision INTEGER NOT NULL, ordinal INTEGER NOT NULL, proposal_json TEXT NOT NULL, retention_until TEXT NOT NULL, PRIMARY KEY(goal_id, goal_revision, ordinal))"
                )
                connection.execute(
                    "INSERT INTO goals VALUES (?, ?, ?, ?, ?)",
                    (
                        "goal-1",
                        1,
                        CONTAINER_RETENTION.isoformat(),
                        json.dumps(_goal_to_data(_goal())),
                        "conversation-1",
                    ),
                )
                connection.execute("PRAGMA user_version = 4")
                connection.commit()
            finally:
                connection.close()
            store = SQLiteGoalStore(path)
            try:
                self.assertIsNone(store.load("goal-1").provenance)
                columns = {
                    row[1]
                    for row in store._connection.execute("PRAGMA table_info(goals)")
                }
                self.assertTrue(
                    {
                        "content_origins",
                        "content_recorded_at",
                        "content_expires_at",
                        "mail_references",
                    }.issubset(columns)
                )
            finally:
                store.close()


class CapabilityResultProvenanceTests(unittest.TestCase):
    """A capability result's provenance must reach the goal it is folded into.

    This is the path where mail content actually enters durable state: AL/X
    reads a message, and the result is written into the goal. If that union is
    dropped, the goal records mail-derived content stamped as AL/X's own work
    and never expires. The other wiring tests do not dispatch a capability, so
    they do not cover this.
    """

    def test_a_mail_read_result_makes_the_goal_mail_derived(self) -> None:
        from alx.contracts import (
            ApprovalLifecycle,
            CapabilityAttempt,
            CapabilityAttemptDisposition,
            CapabilityCall,
            CapabilityResult,
            CapabilityResultState,
        )

        policy = RetentionPolicy()
        with tempfile.TemporaryDirectory() as directory:
            goals_path = Path(directory) / "goals.sqlite3"
            goals = SQLiteGoalStore(goals_path)
            try:
                # A goal that owes nothing to mail: AL/X's own work so far.
                snapshot = goals.create(
                    _goal(),
                    "conversation-1",
                    CONTAINER_RETENTION,
                    policy.non_mail(ContentOrigin.ALX, NOW),
                )
                self.assertFalse(snapshot.provenance.governed_by_retention())

                # Reading a message returns a result carrying mail provenance.
                call = CapabilityCall("call-1", "read_mail_message", {})
                result = CapabilityResult(
                    "call-1",
                    "read_mail_message",
                    CapabilityResultState.SUCCEEDED,
                    {"subject": "Quote"},
                    provenance=policy.direct_mail(NOW, (REFERENCE,)),
                )
                attempt = CapabilityAttempt(
                    call,
                    CapabilityAttemptDisposition.EXECUTED,
                    True,
                    result,
                )
                core = CoreAgent(
                    goals,
                    OneDecision(AgentDecision(response="Read.")),
                    lambda call, state: None,
                    (),
                    clock=lambda: CORE_TIME,
                )
                pending = goals.replace(
                    snapshot.state.__class__(
                        snapshot.state.goal_id,
                        snapshot.state.objective,
                        snapshot.state.success_criteria,
                        attempts=(attempt,),
                    ),
                    CONTAINER_RETENTION,
                    snapshot.revision,
                    snapshot.provenance,
                )
                folded = core._finalize_dispatch(pending, attempt, CORE_TIME)
            finally:
                goals.close()

        self.assertIsNotNone(folded.provenance)
        self.assertIn(ContentOrigin.MAIL_MESSAGE, folded.provenance.origins)
        self.assertEqual(folded.provenance.mail_references, (REFERENCE,))
        self.assertEqual(
            folded.provenance.content_expires_at, NOW + timedelta(days=30)
        )


if __name__ == "__main__":
    unittest.main()
