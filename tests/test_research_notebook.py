"""The notebook keeps research thinking, and stays out of everything else.

Its job is intellectual continuity: what AL/X is investigating, why it interests
her, what she has come to think, and how that thinking changed. These tests
prove it does that across restarts, and prove the four things it must never
become: a second goal store, a second memory store, a second evidence store, or
a path that pours the whole notebook into Core context.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.contracts import (  # noqa: E402
    EntryKind,
    EntryProposal,
    EntryRevisionProposal,
    ResearchQuery,
    ThreadProposal,
    ThreadStatus,
)
from alx.contracts.provenance import ContentOrigin, RetentionPolicy  # noqa: E402
from alx.contracts.mail import MailReference  # noqa: E402
from alx.research import (  # noqa: E402
    ArchivedThreadWrite,
    EntryNotFound,
    EntryRevisionConflict,
    SQLiteResearchStore,
    ThreadNotFound,
)


NOW = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=3)
RETENTION = NOW + timedelta(days=365)


class NotebookTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.path = Path(self._directory.name) / "research.db"
        self.store = SQLiteResearchStore(self.path)

    def tearDown(self) -> None:
        self.store.close()
        self._directory.cleanup()

    def open_thread(self, thread_id: str = "t-1") -> None:
        self.store.open_thread(
            ThreadProposal(
                thread_id=thread_id,
                question="Why do some jellyfish appear not to age?",
                interest="It bothers me that I assumed biological ageing was universal.",
                opened_at=NOW,
            ),
            RETENTION,
        )

    def record(self, entry_id: str, kind: EntryKind, content: str, sources=()) -> None:
        self.store.record_entry(
            EntryProposal(
                entry_id=entry_id,
                thread_id="t-1",
                kind=kind,
                content=content,
                recorded_at=NOW,
                source_references=tuple(sources),
            )
        )


class ThreadAndEntryTest(NotebookTestCase):
    def test_a_thread_records_why_it_interests_her(self) -> None:
        self.open_thread()
        thread = self.store.read_thread("t-1")
        self.assertEqual(
            thread.interest,
            "It bothers me that I assumed biological ageing was universal.",
        )
        self.assertIs(thread.status, ThreadStatus.OPEN)

    def test_interest_is_required(self) -> None:
        """A subject without a reason loses what made the enquiry hers."""
        with self.assertRaises(ValueError):
            ThreadProposal(
                thread_id="t-2", question="Anything?", interest="  ", opened_at=NOW
            )

    def test_doubts_and_questions_are_first_class(self) -> None:
        self.open_thread()
        self.record("e-1", EntryKind.DOUBT, "The claim rests on one lab population.")
        self.record("e-2", EntryKind.QUESTION, "Does this hold outside captivity?")
        kinds = {e.kind for e in self.store.read_thread("t-1").entries}
        self.assertEqual(kinds, {EntryKind.DOUBT, EntryKind.QUESTION})

    def test_there_is_no_complete_status(self) -> None:
        """Research is never declared finished by the storage layer."""
        self.assertEqual(
            {s.value for s in ThreadStatus}, {"open", "paused", "archived"}
        )


class RevisionTest(NotebookTestCase):
    def test_revising_preserves_what_she_thought_before(self) -> None:
        self.open_thread()
        self.record("e-1", EntryKind.CLAIM, "Turritopsis dohrnii is biologically immortal.")
        self.store.revise_entry(
            "e-1",
            EntryRevisionProposal(
                content="Turritopsis dohrnii can revert to a polyp; 'immortal' overstates it.",
                reason="Reverting is not the same as not ageing, and I had conflated them.",
                recorded_at=LATER,
            ),
            expected_revision=1,
        )
        entry = self.store.read_entry("e-1")
        self.assertEqual(entry.revision, 2)
        self.assertEqual(len(entry.revisions), 2)
        self.assertIn("biologically immortal", entry.revisions[0].content)
        self.assertIn("overstates it", entry.current.content)
        self.assertIsNone(entry.revisions[0].reason)
        self.assertIn("conflated", entry.current.reason)

    def test_a_concurrent_revision_is_refused_and_overwrites_nothing(self) -> None:
        self.open_thread()
        self.record("e-1", EntryKind.CLAIM, "First view.")
        self.store.revise_entry(
            "e-1",
            EntryRevisionProposal("Second view.", "new evidence", LATER),
            expected_revision=1,
        )
        with self.assertRaises(EntryRevisionConflict):
            self.store.revise_entry(
                "e-1",
                EntryRevisionProposal("Third view.", "stale", LATER),
                expected_revision=1,
            )
        entry = self.store.read_entry("e-1")
        self.assertEqual(entry.revision, 2)
        self.assertEqual(entry.current.content, "Second view.")

    def test_a_revision_requires_a_reason(self) -> None:
        with self.assertRaises(ValueError):
            EntryRevisionProposal("Changed.", "   ", LATER)


class RestartTest(NotebookTestCase):
    def test_research_survives_restart(self) -> None:
        """A new process gets the thread, the interest and every revision."""
        self.open_thread()
        self.record("e-1", EntryKind.HYPOTHESIS, "Reversion is stress-triggered.")
        self.store.revise_entry(
            "e-1",
            EntryRevisionProposal(
                "Reversion follows starvation specifically.",
                "Narrowed after reading the 1996 paper.",
                LATER,
            ),
            expected_revision=1,
        )
        self.store.close()

        reopened = SQLiteResearchStore(self.path)
        try:
            thread = reopened.read_thread("t-1")
            self.assertEqual(thread.question, "Why do some jellyfish appear not to age?")
            self.assertIn("bothers me", thread.interest)
            entry = reopened.read_entry("e-1")
            self.assertEqual(entry.revision, 2)
            self.assertEqual(len(entry.revisions), 2)
            self.assertIn("stress-triggered", entry.revisions[0].content)
            self.assertIn("starvation", entry.current.content)
        finally:
            reopened.close()

    def test_a_paused_thread_resumes_after_restart(self) -> None:
        self.open_thread()
        self.store.set_status("t-1", ThreadStatus.PAUSED)
        self.store.close()
        reopened = SQLiteResearchStore(self.path)
        try:
            self.assertIs(reopened.read_thread("t-1").status, ThreadStatus.PAUSED)
            reopened.set_status("t-1", ThreadStatus.OPEN)
            self.assertIs(reopened.read_thread("t-1").status, ThreadStatus.OPEN)
        finally:
            reopened.close()


class ScopedRetrievalTest(NotebookTestCase):
    def test_an_unscoped_query_is_refused(self) -> None:
        """There is no list-all path; that is what keeps Core context small."""
        with self.assertRaises(ValueError):
            ResearchQuery(query_id="q-1")

    def test_kind_alone_is_not_a_scope(self) -> None:
        with self.assertRaises(ValueError):
            ResearchQuery(query_id="q-1", kinds=(EntryKind.CLAIM,))

    def test_status_alone_is_not_a_scope(self) -> None:
        with self.assertRaises(ValueError):
            ResearchQuery(query_id="q-1", statuses=(ThreadStatus.OPEN,))

    def test_search_returns_only_the_scoped_thread(self) -> None:
        self.open_thread("t-1")
        self.store.open_thread(
            ThreadProposal("t-2", "Unrelated question", "Unrelated interest", NOW),
            RETENTION,
        )
        self.record("e-1", EntryKind.CLAIM, "In scope.")
        self.store.record_entry(
            EntryProposal("e-2", "t-2", EntryKind.CLAIM, "Out of scope.", NOW)
        )
        found = self.store.retrieve(ResearchQuery(query_id="q-1", thread_ids=("t-1",)))
        self.assertEqual([e.entry_id for e in found], ["e-1"])

    def test_search_by_cited_source(self) -> None:
        self.open_thread()
        self.record("e-1", EntryKind.CLAIM, "Cited.", sources=("evidence-42",))
        self.record("e-2", EntryKind.CLAIM, "Uncited.")
        found = self.store.retrieve(
            ResearchQuery(query_id="q-1", source_references=("evidence-42",))
        )
        self.assertEqual([e.entry_id for e in found], ["e-1"])

    def test_archived_research_stays_out_of_ordinary_retrieval(self) -> None:
        self.open_thread()
        self.record("e-1", EntryKind.CLAIM, "Put aside.")
        self.store.set_status("t-1", ThreadStatus.ARCHIVED)
        query = ResearchQuery(query_id="q-1", thread_ids=("t-1",))
        self.assertEqual(self.store.retrieve(query), ())
        asked = ResearchQuery(
            query_id="q-2", thread_ids=("t-1",), include_archived=True
        )
        self.assertEqual([e.entry_id for e in self.store.retrieve(asked)], ["e-1"])

    def test_a_limit_bounds_what_retrieval_can_return(self) -> None:
        self.open_thread()
        for index in range(10):
            self.record(f"e-{index}", EntryKind.CLAIM, f"Claim {index}.")
        found = self.store.retrieve(
            ResearchQuery(query_id="q-1", thread_ids=("t-1",), limit=3)
        )
        self.assertEqual(len(found), 3)


class DeletionTest(NotebookTestCase):
    def test_deleting_an_entry_removes_every_version_of_it(self) -> None:
        """D-013 defines expiry as logical inaccessibility, not byte erasure.

        The record and all its revisions are gone from the database and
        unreachable through AL/X. Freed pages may still hold the bytes until
        overwritten; D-013 records that secure erasure "remains a separate
        decision", so this asserts what was decided, not more.
        """
        self.open_thread()
        self.record("e-1", EntryKind.CLAIM, "SECRET-CLAIM-CONTENT")
        self.store.revise_entry(
            "e-1",
            EntryRevisionProposal("SECRET-REVISED-CONTENT", "changed", LATER),
            expected_revision=1,
        )
        self.store.delete_entry("e-1", LATER)
        with self.assertRaises(EntryNotFound):
            self.store.read_entry("e-1")
        self.assertEqual(self._rows("research_entry_revisions", "e-1"), 0)

    def test_deleting_a_thread_removes_its_research_content(self) -> None:
        self.open_thread()
        self.record("e-1", EntryKind.CLAIM, "SECRET-ENTRY")
        self.store.delete_thread("t-1", LATER)
        with self.assertRaises(ThreadNotFound):
            self.store.read_thread("t-1")
        self.assertEqual(self._rows("research_threads", "t-1"), 0)
        self.assertEqual(self._rows("research_entry_revisions", "e-1"), 0)

    def test_the_tombstone_carries_no_research_content(self) -> None:
        """An identifier and a time cannot reconstruct what was deleted."""
        self.open_thread()
        self.record("e-1", EntryKind.CLAIM, "SECRET-CLAIM")
        self.store.delete_thread("t-1", LATER)
        records = self.store.deletions()
        self.assertEqual({r.record_id for r in records}, {"t-1", "e-1"})
        for record in records:
            self.assertEqual(
                set(vars(record) if hasattr(record, "__dict__") else
                    {f: getattr(record, f) for f in ("record_id", "kind", "deleted_at")}),
                {"record_id", "kind", "deleted_at"},
            )

    def test_deletion_is_not_archiving(self) -> None:
        """Archiving keeps the record reachable; deletion removes it."""
        self.open_thread()
        self.record("e-1", EntryKind.CLAIM, "ARCHIVED-NOT-DELETED")
        self.store.set_status("t-1", ThreadStatus.ARCHIVED)
        self.assertEqual(self.store.read_thread("t-1").thread_id, "t-1")
        self.store.delete_thread("t-1", LATER)
        with self.assertRaises(ThreadNotFound):
            self.store.read_thread("t-1")

    def test_expired_retention_removes_the_research(self) -> None:
        self.store.open_thread(
            ThreadProposal("t-old", "Old question", "Old interest", NOW),
            NOW + timedelta(days=1),
        )
        self.store.record_entry(
            EntryProposal("e-old", "t-old", EntryKind.CLAIM, "EXPIRING-CONTENT", NOW)
        )
        purged = self.store.purge_expired(NOW + timedelta(days=2))
        self.assertEqual(purged, ("t-old",))
        with self.assertRaises(ThreadNotFound):
            self.store.read_thread("t-old")
        self.assertEqual(self._rows("research_entry_revisions", "e-old"), 0)

    def _rows(self, table: str, identifier: str) -> int:
        """How many rows still hold this record.

        Deletion is proved by absence from the tables, which is what D-013's
        "logical inaccessibility" means. Asserting against raw file bytes would
        be asserting secure erasure, which D-013 reserves for a separate
        decision.
        """
        column = "thread_id" if table == "research_threads" else "entry_id"
        return int(
            self.store._connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (identifier,)
            ).fetchone()[0]
        )


class BoundaryTest(NotebookTestCase):
    def test_the_notebook_is_not_a_second_goal_store(self) -> None:
        """A thread has no objective, criteria, plan or next action."""
        self.open_thread()
        thread = self.store.read_thread("t-1")
        for forbidden in (
            "objective", "success_criteria", "plan", "next_action",
            "blockers", "approvals", "progress", "work_items",
        ):
            self.assertFalse(
                hasattr(thread, forbidden),
                f"a research thread must not carry goal field {forbidden}",
            )

    def test_the_notebook_never_writes_to_memory(self) -> None:
        """Whether research mattered to her is the Core's judgement, not storage."""
        source = (REPOSITORY_ROOT / "src" / "alx" / "research" / "store.py").read_text()
        self.assertNotIn("memories", source)
        self.assertNotIn("MemoryProposal", source)
        self.assertNotIn("autobiographical", source.lower())

    def test_the_notebook_stores_references_not_evidence(self) -> None:
        """Sources are identifiers; nothing copies the evidence itself."""
        self.open_thread()
        self.record("e-1", EntryKind.CLAIM, "Backed by evidence.", sources=("ev-7",))
        entry = self.store.read_entry("e-1")
        self.assertEqual(entry.current.source_references, ("ev-7",))
        # The store has no column and no code that would hold evidence bodies.
        source = (REPOSITORY_ROOT / "src" / "alx" / "research" / "store.py").read_text()
        self.assertNotIn("evidence_body", source)
        self.assertNotIn("evidence_content", source)

    def test_the_notebook_schedules_nothing(self) -> None:
        """No wakeups, no timers, no background work anywhere in the module."""
        for path in (REPOSITORY_ROOT / "src" / "alx" / "research").rglob("*.py"):
            source = path.read_text()
            for forbidden in (
                "import asyncio", "import threading", "import sched",
                "Timer(", "create_task", "call_later",
            ):
                self.assertNotIn(
                    forbidden, source, f"{path.name} must not schedule work"
                )

    def test_archived_research_takes_no_new_thinking(self) -> None:
        self.open_thread()
        self.store.set_status("t-1", ThreadStatus.ARCHIVED)
        with self.assertRaises(ArchivedThreadWrite):
            self.record("e-1", EntryKind.CLAIM, "Too late.")


class ProvenanceTest(NotebookTestCase):
    def test_mail_derived_research_inherits_the_d013_deadline(self) -> None:
        """A citation of mail cannot outlive the message it came from."""
        policy = RetentionPolicy()
        mail = policy.direct_mail(NOW, (MailReference("INBOX", "1", "42"),))
        derived = policy.derive(ContentOrigin.ALX, NOW, (mail,))
        self.open_thread()
        self.store.record_entry(
            EntryProposal(
                "e-1", "t-1", EntryKind.CLAIM, "From a message.", NOW,
                provenance=derived,
            )
        )
        entry = self.store.read_entry("e-1")
        self.assertIsNotNone(entry.current.provenance)
        self.assertTrue(entry.current.provenance.governed_by_retention())
        self.assertEqual(
            entry.current.provenance.content_expires_at, policy.expires_at(NOW)
        )

    def test_provenance_round_trips_through_storage(self) -> None:
        policy = RetentionPolicy()
        provenance = policy.non_mail(ContentOrigin.ALX, NOW)
        self.open_thread()
        self.store.record_entry(
            EntryProposal(
                "e-1", "t-1", EntryKind.CLAIM, "Her own reasoning.", NOW,
                provenance=provenance,
            )
        )
        self.store.close()
        reopened = SQLiteResearchStore(self.path)
        try:
            restored = reopened.read_entry("e-1").current.provenance
            self.assertEqual(restored.origins, frozenset({ContentOrigin.ALX}))
            self.assertIsNone(restored.content_expires_at)
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()
