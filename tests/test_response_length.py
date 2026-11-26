"""Her replies to Friedl are held to a word limit, not just asked to be short (2026-11-13).

One morning's replies ran 71, 78, 116 and 129 words, after the same rule had
been strengthened in her instructions several times. A longer reply is now
refused as a decision; she shortens it in the next step.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (AgentDecision, ConversationOrigin, ConversationSnapshot,  # noqa: E402
                           ConversationTurn)
from alx.core import CoreAgent  # noqa: E402
from alx.core.loop import RESPONSE_WORD_LIMIT, RESPONSE_WORD_REFUSAL  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402

NOW = datetime(2026, 11, 13, 6, 15, tzinfo=UTC)
RETENTION = NOW + timedelta(days=30)


class Queued:
    def __init__(self, *decisions) -> None:
        self.decisions = list(decisions)
        self.contexts = []

    def decide(self, context):
        self.contexts.append(context)
        return self.decisions.pop(0)


def asked() -> ConversationSnapshot:
    return ConversationSnapshot("conversation-1", (
        ConversationTurn("conversation-1", "turn-1", ConversationOrigin.TYPED,
                         "Are C62 and C66 placed as Wurth shows?", NOW, "friedl"),
    ), 1, RETENTION)


class ResponseLengthTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = SQLiteGoalStore(Path(folder.name) / "goals.sqlite3")

    def agent(self, reasoner) -> CoreAgent:
        return CoreAgent(self.store, reasoner, lambda call, state: None, (), clock=lambda: NOW)

    def test_a_long_reply_is_refused_and_the_short_one_is_spoken(self) -> None:
        long = " ".join(["word"] * (RESPONSE_WORD_REFUSAL + 1))
        reasoner = Queued(AgentDecision(response=long),
                          AgentDecision(response="No. Both sit 0.6 mm off Wurth's position. Shall I move them?"))
        outcome = self.agent(reasoner).process(asked(), RETENTION, 4)
        self.assertEqual(outcome.response, "No. Both sit 0.6 mm off Wurth's position. Shall I move them?")
        # She is told why, so the next step can correct it.
        self.assertIn("response_over_%d_words" % RESPONSE_WORD_REFUSAL, repr(reasoner.contexts[1]))

    def test_when_she_judges_he_asked_for_detail_a_long_reply_is_spoken(self) -> None:
        # Friedl, 13 Nov: no hard cap. Whether he asked for detail is her
        # judgement, never a word list over his message (Law 1).
        long = " ".join(["word"] * (RESPONSE_WORD_LIMIT + 30))
        outcome = self.agent(Queued(AgentDecision(response=long, detail_asked=True))).process(
            asked(), RETENTION, 4)
        self.assertEqual(outcome.response, long)

    def test_a_reply_a_few_words_over_the_aim_is_spoken(self) -> None:
        """26 Nov: replies of 31-38 words were refused 14 times in a morning, each a full call."""
        near = " ".join(["word"] * (RESPONSE_WORD_LIMIT + 8))
        outcome = self.agent(Queued(AgentDecision(response=near))).process(asked(), RETENTION, 4)
        self.assertEqual(outcome.response, near)

    def test_a_reply_at_the_limit_is_spoken(self) -> None:
        exact = " ".join(["word"] * RESPONSE_WORD_REFUSAL)
        outcome = self.agent(Queued(AgentDecision(response=exact))).process(asked(), RETENTION, 4)
        self.assertEqual(outcome.response, exact)


    def test_a_corrected_draft_is_not_shown_as_refused(self) -> None:
        events = []
        long = " ".join(["word"] * (RESPONSE_WORD_REFUSAL + 1))
        reasoner = Queued(AgentDecision(response=long), AgentDecision(response="Done. Shall I write it back?"))
        agent = CoreAgent(self.store, reasoner, lambda call, state: None, (), clock=lambda: NOW,
                          trace=events.append)
        self.assertEqual(agent.process(asked(), RETENTION, 4).response, "Done. Shall I write it back?")
        self.assertEqual([e for e in events if e.label == "Step refused"], [])

    def test_a_draft_refused_twice_is_shown(self) -> None:
        events = []
        long = " ".join(["word"] * (RESPONSE_WORD_REFUSAL + 1))
        reasoner = Queued(AgentDecision(response=long), AgentDecision(response=long),
                          AgentDecision(response="I couldn't put that briefly."))
        agent = CoreAgent(self.store, reasoner, lambda call, state: None, (), clock=lambda: NOW,
                          trace=events.append)
        agent.process(asked(), RETENTION, 4)
        self.assertEqual(len([e for e in events if e.label == "Step refused"]), 1)

if __name__ == "__main__":
    unittest.main()
