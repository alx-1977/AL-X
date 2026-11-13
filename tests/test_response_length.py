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
from alx.core.loop import RESPONSE_WORD_LIMIT  # noqa: E402
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
        long = " ".join(["word"] * (RESPONSE_WORD_LIMIT + 1))
        reasoner = Queued(AgentDecision(response=long),
                          AgentDecision(response="No. Both sit 0.6 mm off Wurth's position. Shall I move them?"))
        outcome = self.agent(reasoner).process(asked(), RETENTION, 4)
        self.assertEqual(outcome.response, "No. Both sit 0.6 mm off Wurth's position. Shall I move them?")
        # She is told why, so the next step can correct it.
        self.assertIn("response_over_%d_words" % RESPONSE_WORD_LIMIT, repr(reasoner.contexts[1]))

    def test_a_reply_at_the_limit_is_spoken(self) -> None:
        exact = " ".join(["word"] * RESPONSE_WORD_LIMIT)
        outcome = self.agent(Queued(AgentDecision(response=exact))).process(asked(), RETENTION, 4)
        self.assertEqual(outcome.response, exact)


if __name__ == "__main__":
    unittest.main()
