"""Retrieved candidates accumulate across one reasoning process.

A second retrieval used to replace the first. Reasoning that legitimately
needed two of them could therefore only ever see the later one, and the memory
it had already found came back as nothing — so the same retrieval would be
asked for again, spending a step to rediscover what the turn already knew.

Accumulation is deliberately narrow. Results join by `memory_id` and nothing
else: two retrievals overlap all the time, and the same memory twice is the
same memory. Asking whether two *different* memories mean the same thing is a
judgement, and this code does not make judgements.

The set stays ephemeral. It lives in the process, is never written back, and
goes when the turn goes — retrieved candidates are not memories, and promoting
them automatically would be a second memory system nobody decided to build.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    AgentDecision,
    ConversationOrigin,
    ConversationSnapshot,
    ConversationTurn,
    MemoryKind,
    MemoryProposal,
    MemoryQuery,
)
from alx.core import CoreAgent, CoreState  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402
from alx.memories import SQLiteMemoryStore  # noqa: E402

NOW = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
RETENTION = NOW + timedelta(days=30)
FACTUAL = (MemoryKind.FACTUAL,)


class Queued:
    """A fake Core model that records what each step was shown."""

    def __init__(self, *decisions) -> None:
        self.decisions = list(decisions)
        self.contexts = []

    def decide(self, context):
        self.contexts.append(context)
        return self.decisions.pop(0)


def conversation() -> ConversationSnapshot:
    return ConversationSnapshot(
        "conv-1",
        (
            ConversationTurn(
                "conv-1", "turn-1", ConversationOrigin.TYPED,
                "what do we know about the board?", NOW, person_id="friedl",
            ),
        ),
        1,
        RETENTION,
    )


class AccumulationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.goals = SQLiteGoalStore(root / "goals.sqlite3")
        self.memories = SQLiteMemoryStore(root / "memories.sqlite3")
        self.addCleanup(self.goals.close)
        self.addCleanup(self.memories.close)
        for identifier, content in (
            ("antenna", "the PN532 antenna matched at 13.56 MHz"),
            ("copper", "the copper weight was specified as 1/3 oz"),
        ):
            self.memories.remember(
                MemoryProposal(
                    identifier, MemoryKind.FACTUAL, content,
                    (f"turn:{identifier}",), NOW,
                ),
                RETENTION,
            )

    def agent(self, reasoner) -> CoreAgent:
        return CoreAgent(
            self.goals, reasoner, lambda call, state: None, (),
            memory_store=self.memories, clock=lambda: NOW,
            identifier_factory=lambda: "goal-1",
        )

    def seen_by_final_step(self, reasoner) -> list[str]:
        """The memories the last reasoning step was shown."""
        return [item.memory_id for item in reasoner.contexts[-1].memories]

    def test_a_second_retrieval_keeps_what_the_first_found(self) -> None:
        """The defect: reasoning could hold only one retrieval at a time."""
        reasoner = Queued(
            AgentDecision(memory_query=MemoryQuery("q1", FACTUAL, topic="antenna")),
            AgentDecision(memory_query=MemoryQuery("q2", FACTUAL, topic="copper")),
            AgentDecision(response="both are on record."),
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 5)
        self.assertIs(outcome.state, CoreState.RESPONDED)
        self.assertEqual(sorted(self.seen_by_final_step(reasoner)), ["antenna", "copper"])

    def test_overlapping_retrievals_do_not_duplicate_a_memory(self) -> None:
        """Identity deduplication, and nothing cleverer than identity."""
        reasoner = Queued(
            AgentDecision(memory_query=MemoryQuery("q1", FACTUAL, topic="antenna")),
            AgentDecision(
                memory_query=MemoryQuery("q2", FACTUAL, memory_ids=("antenna",))
            ),
            AgentDecision(response="noted."),
        )
        self.agent(reasoner).process(conversation(), RETENTION, 5)
        seen = self.seen_by_final_step(reasoner)
        self.assertEqual(seen.count("antenna"), 1)
        self.assertEqual(seen, ["antenna"])

    def test_the_first_retrieval_is_visible_to_the_next_step(self) -> None:
        reasoner = Queued(
            AgentDecision(memory_query=MemoryQuery("q1", FACTUAL, topic="antenna")),
            AgentDecision(response="found it."),
        )
        self.agent(reasoner).process(conversation(), RETENTION, 5)
        self.assertEqual(
            [item.memory_id for item in reasoner.contexts[1].memories], ["antenna"]
        )

    def test_the_first_step_is_shown_nothing(self) -> None:
        """Retrieval is a move she makes, never a prefix she is handed."""
        reasoner = Queued(AgentDecision(response="nothing needed."))
        self.agent(reasoner).process(conversation(), RETENTION, 5)
        self.assertEqual(reasoner.contexts[0].memories, ())

    def test_candidates_are_not_persisted_as_memories(self) -> None:
        """Retrieving is not remembering. Nothing new may be written."""
        before = {
            item.memory_id
            for item in self.memories.list_memories(MemoryKind.FACTUAL)
        }
        reasoner = Queued(
            AgentDecision(memory_query=MemoryQuery("q1", FACTUAL, topic="antenna")),
            AgentDecision(memory_query=MemoryQuery("q2", FACTUAL, topic="copper")),
            AgentDecision(response="noted."),
        )
        self.agent(reasoner).process(conversation(), RETENTION, 5)
        after = {
            item.memory_id
            for item in self.memories.list_memories(MemoryKind.FACTUAL)
        }
        self.assertEqual(before, after)

    def test_candidates_do_not_survive_the_process(self) -> None:
        """The set is turn-local: a later turn starts from nothing again."""
        first = Queued(
            AgentDecision(memory_query=MemoryQuery("q1", FACTUAL, topic="antenna")),
            AgentDecision(response="found it."),
        )
        agent = self.agent(first)
        agent.process(conversation(), RETENTION, 5)

        second = Queued(AgentDecision(response="a new turn."))
        CoreAgent(
            self.goals, second, lambda call, state: None, (),
            memory_store=self.memories, clock=lambda: NOW,
            identifier_factory=lambda: "goal-1",
        ).process(conversation(), RETENTION, 5)
        self.assertEqual(second.contexts[0].memories, ())

    def test_a_reused_query_identifier_is_still_refused(self) -> None:
        """Accumulation must not have weakened the existing repeat guard."""
        query = MemoryQuery("q1", FACTUAL, topic="antenna")
        reasoner = Queued(
            AgentDecision(memory_query=query),
            AgentDecision(memory_query=query),
            AgentDecision(response="noted."),
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 5)
        self.assertIs(outcome.state, CoreState.RESPONDED)
        self.assertEqual(self.seen_by_final_step(reasoner), ["antenna"])


if __name__ == "__main__":
    unittest.main()
