"""Stage 2A: bounded topic retrieval, and the boundaries it must not cross.

Topic is the first dimension of memory retrieval that ranks rather than bounds,
and that makes it the first one that could quietly undo a guard. These tests
hold the two properties that keep it safe.

A topic narrows: it is allowed to satisfy the anti-dump guard only because
every retrieval is capped, so the answer to a vague topic is the best few and
never the store. `project_id` deliberately does not satisfy that guard, because
a project accumulates indefinitely.

A topic orders, it never widens: every deterministic constraint — person,
project, kind, date, source, supersession — decides what is eligible before a
topic decides what comes first. A memory in another project, or belonging to
another person, cannot surface by matching words well.

The FTS index is derived. Dropping it must cost ranking and never a memory, so
a rebuild is proved to reproduce retrieval from the authoritative rows alone.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.contracts import (  # noqa: E402
    MAX_MEMORY_RETRIEVAL_LIMIT,
    MemoryKind,
    MemoryMatchReason,
    MemoryProposal,
    MemoryQuery,
    MemorySupersession,
    ScopeReference,
)
from alx.memories import SQLiteMemoryStore  # noqa: E402

NOW = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
RETENTION = NOW + timedelta(days=30)
FACTUAL = (MemoryKind.FACTUAL,)


class MemoryStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "memories.sqlite3"
        self.store = SQLiteMemoryStore(self.path)
        self.addCleanup(self.store.close)

    def remember(
        self,
        memory_id: str,
        content: str,
        *,
        project_id: str | None = None,
        kind: MemoryKind = MemoryKind.FACTUAL,
        person_id: str | None = None,
        supersedes: str | None = None,
    ) -> None:
        self.store.remember(
            MemoryProposal(
                memory_id,
                kind,
                content,
                (f"turn:{memory_id}",),
                NOW,
                person_id=person_id,
                supersedes_memory_id=supersedes,
                scope=(
                    None if project_id is None else ScopeReference(project_id=project_id)
                ),
            ),
            RETENTION,
        )

    def ids(self, query: MemoryQuery) -> list[str]:
        return [item.memory_id for item in self.store.retrieve(query, NOW)]


class AntiDumpGuardTests(unittest.TestCase):
    """What may stand alone as a retrieval scope, and what may not."""

    def test_a_topic_is_a_sufficient_scope(self) -> None:
        """The September 4 failure: she was refused for asking her own memory.

        Twice in thirty seconds a live turn ended on `retrieval requires a
        scope narrower than memory kind alone`, because the only scopes
        available were identifiers and dates she did not have. A topic is the
        narrowing that question actually has.
        """
        query = MemoryQuery("q", FACTUAL, topic="the antenna")
        self.assertEqual(query.topic, "the antenna")

    def test_kinds_alone_are_still_refused(self) -> None:
        """The broad-dump protection the topic must not weaken."""
        with self.assertRaises(ValueError) as caught:
            MemoryQuery("q", FACTUAL)
        self.assertIn("narrower than memory kind alone", str(caught.exception))

    def test_a_project_alone_is_not_a_scope(self) -> None:
        """A project accumulates, so "everything in it" is the same replay."""
        with self.assertRaises(ValueError) as caught:
            MemoryQuery("q", FACTUAL, project_id="pn532")
        self.assertIn("narrower than memory kind alone", str(caught.exception))

    def test_a_blank_topic_is_not_a_topic(self) -> None:
        """Whitespace must not pass the guard and then match everything."""
        for value in ("", "   ", "\t\n"):
            with self.subTest(topic=value):
                with self.assertRaises(ValueError):
                    MemoryQuery("q", FACTUAL, topic=value)

    def test_a_topic_is_normalised(self) -> None:
        self.assertEqual(
            MemoryQuery("q", FACTUAL, topic="  the   antenna  ").topic, "the antenna"
        )

    def test_every_retrieval_is_bounded(self) -> None:
        """Topic is allowed to narrow only because the result is capped."""
        self.assertEqual(
            MemoryQuery("q", FACTUAL, topic="antenna").limit,
            MAX_MEMORY_RETRIEVAL_LIMIT,
        )
        with self.assertRaises(ValueError):
            MemoryQuery("q", FACTUAL, topic="antenna", limit=MAX_MEMORY_RETRIEVAL_LIMIT + 1)
        for value in (0, -1):
            with self.subTest(limit=value):
                with self.assertRaises(ValueError):
                    MemoryQuery("q", FACTUAL, topic="antenna", limit=value)

    def test_a_person_scope_may_be_completed_by_a_topic(self) -> None:
        """Relationship plus another kind needs its own scope; topic is one."""
        query = MemoryQuery(
            "q",
            (MemoryKind.RELATIONSHIP, MemoryKind.FACTUAL),
            person_id="friedl",
            topic="soldering",
        )
        self.assertEqual(query.topic, "soldering")


class TopicRetrievalTests(MemoryStoreTestCase):
    """A topic orders the eligible memories. It never adds one."""

    def test_a_topic_finds_a_memory_about_it(self) -> None:
        self.remember("a", "the PN532 antenna matched at 13.56 MHz")
        self.remember("b", "the invoice from DigiKey arrived")
        self.assertEqual(self.ids(MemoryQuery("q", FACTUAL, topic="antenna")), ["a"])

    def test_a_topic_requires_every_term(self) -> None:
        """Conservative by choice: asking about two things means both."""
        self.remember("a", "the antenna impedance was measured")
        self.remember("b", "the antenna was fitted")
        self.assertEqual(
            self.ids(MemoryQuery("q", FACTUAL, topic="antenna impedance")), ["a"]
        )

    def test_a_topic_matching_nothing_returns_nothing(self) -> None:
        self.remember("a", "the antenna matched")
        self.assertEqual(self.ids(MemoryQuery("q", FACTUAL, topic="oscilloscope")), [])

    def test_retrieval_is_capped_by_limit(self) -> None:
        for index in range(10):
            self.remember(f"m{index}", f"antenna measurement number {index}")
        self.assertEqual(
            len(self.ids(MemoryQuery("q", FACTUAL, topic="antenna", limit=3))), 3
        )

    def test_search_syntax_in_a_topic_is_inert(self) -> None:
        """Her words are a topic, never an expression the backend obeys."""
        self.remember("a", "the antenna matched")
        self.remember("b", "the copper weight was wrong")
        for topic in ('antenna OR copper', 'antenna*', 'antenna"', "antenna NEAR copper"):
            with self.subTest(topic=topic):
                found = self.ids(MemoryQuery("q", FACTUAL, topic=topic))
                self.assertNotIn("b", found)

    def test_match_reason_says_why_without_naming_a_mechanism(self) -> None:
        self.remember("a", "the antenna matched")
        topic = self.store.retrieve(MemoryQuery("q1", FACTUAL, topic="antenna"), NOW)
        exact = self.store.retrieve(
            MemoryQuery("q2", FACTUAL, memory_ids=("a",)), NOW
        )
        dated = self.store.retrieve(
            MemoryQuery("q3", FACTUAL, formed_after=NOW - timedelta(days=1)), NOW
        )
        self.assertIs(topic[0].match_reason, MemoryMatchReason.TOPIC)
        self.assertIs(exact[0].match_reason, MemoryMatchReason.EXACT)
        self.assertIs(dated[0].match_reason, MemoryMatchReason.SCOPE)


class ProjectBoundaryTests(MemoryStoreTestCase):
    """A project is a boundary, never a preference."""

    def test_a_better_match_in_another_project_does_not_surface(self) -> None:
        """The property that stops similarity outranking a deterministic scope."""
        self.remember("other", "antenna antenna antenna tuning notes", project_id="water")
        self.remember("mine", "the antenna was fitted", project_id="pn532")
        self.assertEqual(
            self.ids(MemoryQuery("q", FACTUAL, topic="antenna", project_id="pn532")),
            ["mine"],
        )

    def test_an_unscoped_memory_does_not_satisfy_a_project_scope(self) -> None:
        self.remember("loose", "the antenna was fitted")
        self.assertEqual(
            self.ids(MemoryQuery("q", FACTUAL, topic="antenna", project_id="pn532")), []
        )

    def test_omitting_the_project_searches_across_them(self) -> None:
        """Cross-project recall stays possible when she intends it."""
        self.remember("a", "the antenna was fitted", project_id="pn532")
        self.remember("b", "the antenna was ordered", project_id="water")
        self.remember("c", "the antenna arrived")
        self.assertEqual(
            sorted(self.ids(MemoryQuery("q", FACTUAL, topic="antenna"))),
            ["a", "b", "c"],
        )


class PersonIsolationTests(MemoryStoreTestCase):
    """Isolation is `person_id` alone, and a topic cannot reach past it."""

    def test_a_topic_cannot_cross_a_person_boundary(self) -> None:
        self.remember(
            "friedl",
            "prefers the bench lamp on the left",
            kind=MemoryKind.RELATIONSHIP,
            person_id="friedl",
        )
        self.remember(
            "other",
            "prefers the bench lamp on the right",
            kind=MemoryKind.RELATIONSHIP,
            person_id="someone-else",
        )
        found = self.ids(
            MemoryQuery(
                "q",
                (MemoryKind.RELATIONSHIP,),
                person_id="friedl",
                topic="bench lamp",
            )
        )
        self.assertEqual(found, ["friedl"])

    def test_a_relationship_memory_is_reachable_across_projects(self) -> None:
        """A working preference is not confined to the work it arose in."""
        self.remember(
            "pref",
            "prefers metric fasteners throughout",
            kind=MemoryKind.RELATIONSHIP,
            person_id="friedl",
            project_id="pn532",
        )
        found = self.ids(
            MemoryQuery(
                "q",
                (MemoryKind.RELATIONSHIP,),
                person_id="friedl",
                topic="metric fasteners",
            )
        )
        self.assertEqual(found, ["pref"])


class SupersessionTests(MemoryStoreTestCase):
    """Retrieval reports history. It never resolves it."""

    def setUp(self) -> None:
        super().setUp()
        self.remember("old", "we chose 1/3 oz copper weight for the board")
        self.remember(
            "new",
            "the 1/3 oz copper weight was a manufacturing mistake",
            supersedes="old",
        )

    def test_history_is_left_out_unless_asked_for(self) -> None:
        found = self.store.retrieve(MemoryQuery("q", FACTUAL, topic="copper"), NOW)
        self.assertEqual([item.memory_id for item in found], ["new"])
        self.assertIs(found[0].supersession, MemorySupersession.CURRENT)

    def test_both_are_returned_and_labelled_when_history_is_asked_for(self) -> None:
        """Neither is corrected for her: the contradiction is hers to read."""
        found = self.store.retrieve(
            MemoryQuery("q", FACTUAL, topic="copper", include_superseded=True), NOW
        )
        states = {item.memory_id: item.supersession for item in found}
        self.assertEqual(
            states,
            {
                "old": MemorySupersession.SUPERSEDED,
                "new": MemorySupersession.CURRENT,
            },
        )

    def test_a_superseded_memory_stays_reachable_by_identifier(self) -> None:
        found = self.store.retrieve(
            MemoryQuery(
                "q", FACTUAL, memory_ids=("old",), include_superseded=True
            ),
            NOW,
        )
        self.assertEqual([item.memory_id for item in found], ["old"])
        self.assertIs(found[0].supersession, MemorySupersession.SUPERSEDED)


class DerivedIndexTests(MemoryStoreTestCase):
    """The index is an accelerator. Losing it must not lose a memory."""

    def test_a_rebuild_reproduces_retrieval_from_the_memories(self) -> None:
        self.remember("a", "the antenna matched at 13.56 MHz")
        self.remember("b", "the copper weight was wrong")
        before = self.ids(MemoryQuery("q1", FACTUAL, topic="antenna"))
        self.store.rebuild_topic_index()
        after = self.ids(MemoryQuery("q2", FACTUAL, topic="antenna"))
        self.assertEqual(before, after)
        self.assertEqual(before, ["a"])

    def test_a_rebuild_destroys_no_memory(self) -> None:
        self.remember("a", "the antenna matched at 13.56 MHz")
        self.store.rebuild_topic_index()
        self.assertEqual(
            self.store.load("a").current.content, "the antenna matched at 13.56 MHz"
        )

    def test_exact_retrieval_survives_a_missing_index(self) -> None:
        """A deterministic scope must not depend on a derived table."""
        self.remember("a", "the antenna matched at 13.56 MHz")
        self.store._connection.execute("DROP TABLE memory_topics")
        found = self.store.retrieve(MemoryQuery("q", FACTUAL, memory_ids=("a",)), NOW)
        self.assertEqual([item.memory_id for item in found], ["a"])

    def test_a_missing_index_costs_ranking_not_memories(self) -> None:
        """Degrade, never deny: the rows are still what is remembered."""
        self.remember("a", "the antenna matched at 13.56 MHz")
        self.store._connection.execute("DROP TABLE memory_topics")
        found = self.store.retrieve(MemoryQuery("q", FACTUAL, topic="antenna"), NOW)
        self.assertEqual([item.memory_id for item in found], ["a"])
        self.assertIs(found[0].match_reason, MemoryMatchReason.SCOPE)

    def test_a_correction_updates_what_the_index_describes(self) -> None:
        from alx.contracts import MemoryCorrection

        self.remember("a", "the antenna matched at 13.56 MHz")
        self.store.correct(
            "a",
            MemoryCorrection(
                "the oscillator matched at 13.56 MHz",
                "measured again",
                ("turn:2",),
                NOW,
            ),
            1,
        )
        self.assertEqual(self.ids(MemoryQuery("q1", FACTUAL, topic="oscillator")), ["a"])
        self.assertEqual(self.ids(MemoryQuery("q2", FACTUAL, topic="antenna")), [])

    def test_a_deleted_memory_leaves_the_index(self) -> None:
        self.remember("a", "the antenna matched at 13.56 MHz")
        self.store.delete("a", 1)
        self.assertEqual(self.ids(MemoryQuery("q", FACTUAL, topic="antenna")), [])

    def test_the_index_is_not_the_memory(self) -> None:
        """Authoritative rows are the truth; the index only points at them."""
        self.remember("a", "the antenna matched at 13.56 MHz")
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        stored = connection.execute(
            "SELECT COUNT(*) FROM memories WHERE memory_id = 'a'"
        ).fetchone()[0]
        self.assertEqual(stored, 1)


class ExactRetrievalUnchangedTests(MemoryStoreTestCase):
    """Everything that worked before topic retrieval still works."""

    def test_identifier_retrieval_is_unchanged(self) -> None:
        self.remember("a", "first")
        self.remember("b", "second")
        self.assertEqual(self.ids(MemoryQuery("q", FACTUAL, memory_ids=("b",))), ["b"])

    def test_source_retrieval_is_unchanged(self) -> None:
        self.remember("a", "first")
        self.assertEqual(
            self.ids(MemoryQuery("q", FACTUAL, source_references=("turn:a",))), ["a"]
        )

    def test_date_retrieval_is_unchanged(self) -> None:
        self.remember("a", "first")
        self.assertEqual(
            self.ids(
                MemoryQuery("q", FACTUAL, formed_after=NOW - timedelta(minutes=1))
            ),
            ["a"],
        )

    def test_kind_filtering_is_unchanged(self) -> None:
        self.remember("fact", "the antenna matched")
        self.remember(
            "pref",
            "prefers the antenna mounted flat",
            kind=MemoryKind.RELATIONSHIP,
            person_id="friedl",
        )
        self.assertEqual(self.ids(MemoryQuery("q", FACTUAL, topic="antenna")), ["fact"])


if __name__ == "__main__":
    unittest.main()
