"""A background occasion sees what was just said in Friedl's conversation.

2026-11-10/11: she repeated parcel news already given to him, and a mail
question was put to him while her own question was still open, so his
"yes please" answered the wrong one. Each background occasion reasoned only
over its own thread.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import AgentDecision, CognitionOrigin, ConversationOrigin, ConversationTurn  # noqa: E402
from alx.conversation import ConversationGateway, SQLiteConversationStore  # noqa: E402
from alx.core import CoreAgent  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402

NOW = datetime(2026, 11, 11, 11, 0, tzinfo=UTC)
RETENTION = NOW + timedelta(days=30)


class Reasoner:
    def __init__(self, *decisions):
        self.decisions = list(decisions)
        self.contexts = []

    def decide(self, context):
        self.contexts.append(context)
        return self.decisions.pop(0)


class RecentWithFriedlTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.goals = SQLiteGoalStore(root / "goals.sqlite3")
        self.conversations = SQLiteConversationStore(root / "conversations.sqlite3")
        self.addCleanup(self.goals.close)
        self.addCleanup(self.conversations.close)
        self.now = [NOW - timedelta(hours=5)]

    def gateway(self, reasoner, live):
        core = CoreAgent(self.goals, reasoner, lambda call, state: None, (),
                         clock=lambda: self.now[0])
        ids = iter(f"alx-{n}" for n in range(20))
        return ConversationGateway(core, self.conversations, identifier_factory=lambda: next(ids),
                                   clock=lambda: self.now[0], live_thread=lambda: live)

    def say(self, gateway, turn_id, text):
        gateway.receive_conversation_turn(ConversationTurn(
            "live", turn_id, ConversationOrigin.TYPED, text, self.now[0], "friedl"), 1, RETENTION)

    def opportunity(self):
        return SimpleNamespace(opportunity_id="opp-1", arose_at=self.now[0],
                               origin=CognitionOrigin.EXTERNAL_EVENT, references=(),
                               note=None, provenance=None)

    def recent(self, context):
        return [event for event in context.events if event.kind == "conversation.recent_with_friedl"]

    def test_a_mail_occasion_sees_what_she_just_told_him(self) -> None:
        reasoner = Reasoner(AgentDecision(response="An old exchange."),
                            AgentDecision(response="The JLC parcel was delivered at 07:50."),
                            AgentDecision(finish_silently=True))
        gateway = self.gateway(reasoner, "live")
        self.say(gateway, "t-old", "Morning")          # five hours ago: outside the window
        self.now[0] = NOW - timedelta(minutes=10)
        self.say(gateway, "t-1", "Where is the JLC parcel?")
        self.now[0] = NOW
        gateway.receive_cognition_opportunity("mail-thread", self.opportunity(), 2, RETENTION)
        [event] = self.recent(reasoner.contexts[-1])
        turns = event.transient_data["turns"]
        self.assertEqual([turn["text"] for turn in turns],
                         ["Where is the JLC parcel?", "The JLC parcel was delivered at 07:50."])
        self.assertEqual([turn["from"] for turn in turns], ["friedl", "alx"])
        # Read only: nothing is written into the mail thread from his.
        self.assertEqual(len(self.conversations.load("mail-thread").turns), 0)

    def test_his_own_conversation_and_no_screen_add_nothing(self) -> None:
        reasoner = Reasoner(AgentDecision(response="Hi."), AgentDecision(finish_silently=True),
                            AgentDecision(finish_silently=True))
        self.now[0] = NOW
        live = self.gateway(reasoner, "live")
        self.say(live, "t-1", "Hello")
        live.receive_cognition_opportunity("live", self.opportunity(), 2, RETENTION)
        self.assertEqual(self.recent(reasoner.contexts[-1]), [])
        self.gateway(reasoner, None).receive_cognition_opportunity(
            "mail-thread", self.opportunity(), 2, RETENTION)
        self.assertEqual(self.recent(reasoner.contexts[-1]), [])


if __name__ == "__main__":
    unittest.main()
