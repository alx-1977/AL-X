from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    AgentDecision, ConversationOrigin, ConversationTurn,
)
from alx.conversation import (  # noqa: E402
    ConversationGateway, ConversationNotFound, ConversationRevisionConflict,
    SQLiteConversationStore,
)
from alx.core import CoreAgent, CoreState  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402

NOW = datetime(2026, 8, 27, tzinfo=UTC)
RETENTION = NOW + timedelta(days=30)


class QueuedReasoner:
    def __init__(self, *decisions) -> None:
        self.decisions = list(decisions)
        self.contexts = []

    def decide(self, context):
        self.contexts.append(context)
        return self.decisions.pop(0)


class ConversationGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.goals = SQLiteGoalStore(root / "goals.sqlite3")
        self.conversations = SQLiteConversationStore(root / "conversations.sqlite3")

    def tearDown(self) -> None:
        self.conversations.close()
        self.goals.close()
        self.directory.cleanup()

    def gateway(self, reasoner, locator=lambda conversation_id: None):
        core = CoreAgent(self.goals, reasoner, lambda call, state: None, ())
        identifiers = iter(("alx-1", "alx-2", "alx-3"))
        return ConversationGateway(
            core, self.conversations, locator,
            identifier_factory=lambda: next(identifiers), clock=lambda: NOW,
        )

    def test_ordinary_conversation_is_durable_without_a_goal(self) -> None:
        reasoner = QueuedReasoner(AgentDecision(response="Hello Friedl."))
        gateway = self.gateway(reasoner)
        turn = ConversationTurn(
            "conversation-1", "turn-1", ConversationOrigin.SPEECH_TRANSCRIPT,
            "Hello ALX", NOW, "friedl",
        )
        outcome = gateway.receive_conversation_turn(turn, 1, RETENTION)
        recovered = self.conversations.load("conversation-1")
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertIsNone(outcome.snapshot)
        self.assertEqual(
            [item.origin for item in recovered.turns],
            [ConversationOrigin.SPEECH_TRANSCRIPT, ConversationOrigin.ALX_RESPONSE],
        )
        self.assertEqual(recovered.turns[-1].content, "Hello Friedl.")

    def test_follow_up_uses_same_thread_and_same_core_without_goal(self) -> None:
        reasoner = QueuedReasoner(
            AgentDecision(response="first"), AgentDecision(response="second")
        )
        gateway = self.gateway(reasoner)
        first = ConversationTurn("conversation-1", "turn-1", ConversationOrigin.TYPED,
                                 "First", NOW, "friedl")
        second = ConversationTurn("conversation-1", "turn-2", ConversationOrigin.SPEECH_TRANSCRIPT,
                                  "Follow-up", NOW + timedelta(minutes=1), "friedl")
        gateway.receive_conversation_turn(first, 1, RETENTION)
        gateway.receive_conversation_turn(second, 1, RETENTION)
        self.assertEqual(len(reasoner.contexts), 2)
        self.assertEqual(len(reasoner.contexts[1].turns), 3)
        self.assertEqual(reasoner.contexts[1].turns[-1], second)

    def test_user_turn_survives_reasoning_failure(self) -> None:
        class Failure:
            def decide(self, context):
                raise RuntimeError("offline")

        turn = ConversationTurn("conversation-1", "turn-1", ConversationOrigin.TYPED,
                                "Keep this", NOW)
        outcome = self.gateway(Failure()).receive_conversation_turn(turn, 1, RETENTION)
        self.assertEqual(outcome.reason, "reasoner_error")
        self.assertEqual(self.conversations.load("conversation-1").turns, (turn,))

    def test_restart_revision_and_delete_controls_are_independent_of_goals(self) -> None:
        created = self.conversations.create("conversation-1", RETENTION)
        turn = ConversationTurn("conversation-1", "turn-1", ConversationOrigin.TYPED,
                                "Durable", NOW)
        saved = self.conversations.append(turn, RETENTION, created.revision)
        stale = SQLiteConversationStore(Path(self.directory.name) / "conversations.sqlite3")
        try:
            with self.assertRaises(ConversationRevisionConflict):
                stale.append(
                    ConversationTurn("conversation-1", "turn-2", ConversationOrigin.TYPED,
                                     "Stale", NOW), RETENTION, created.revision,
                )
        finally:
            stale.close()
        self.conversations.close()
        self.conversations = SQLiteConversationStore(
            Path(self.directory.name) / "conversations.sqlite3"
        )
        self.assertEqual(self.conversations.load("conversation-1").turns, (turn,))
        self.conversations.delete("conversation-1", saved.revision)
        with self.assertRaises(ConversationNotFound):
            self.conversations.load("conversation-1")


if __name__ == "__main__":
    unittest.main()
