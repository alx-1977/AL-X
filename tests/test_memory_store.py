from __future__ import annotations

import tempfile
import unittest
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alx.contracts import (
    MemoryCorrection,
    MemoryKind,
    MemoryProposal,
    MemoryQuery,
    MemorySourceMatch,
)
from alx.memories import (
    InvalidMemorySupersession,
    MemoryIdentityConflict,
    MemoryNotFound,
    MemoryRevisionConflict,
    SQLiteMemoryStore,
    SupersededMemoryNotFound,
)


NOW = datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc)


def proposal(
    memory_id: str,
    kind: MemoryKind = MemoryKind.AUTOBIOGRAPHICAL,
    *,
    person_id: str | None = None,
    supersedes: str | None = None,
) -> MemoryProposal:
    return MemoryProposal(
        memory_id=memory_id,
        kind=kind,
        content="I changed my view after examining the real result.",
        source_references=("conversation:turn-7", "goal:goal-2:evidence-4"),
        formed_at=NOW,
        person_id=person_id,
        meaning="The experience made me more willing to challenge an assumption." if kind is MemoryKind.AUTOBIOGRAPHICAL else None,
        supersedes_memory_id=supersedes,
    )


class MemoryContractTests(unittest.TestCase):
    def test_autobiographical_memory_requires_provenance_and_meaning(self) -> None:
        with self.assertRaises(ValueError):
            MemoryProposal("memory-1", MemoryKind.AUTOBIOGRAPHICAL, "reflection", (), NOW, meaning="mattered")
        with self.assertRaises(ValueError):
            MemoryProposal("memory-1", MemoryKind.AUTOBIOGRAPHICAL, "reflection", ("turn-1",), NOW)

    def test_relationship_memory_requires_person_isolation(self) -> None:
        with self.assertRaises(ValueError):
            MemoryProposal("memory-1", MemoryKind.RELATIONSHIP, "preference", ("turn-1",), NOW)

    def test_contract_has_no_significance_scoring_fields(self) -> None:
        names = {item.name for item in fields(MemoryProposal)}
        self.assertTrue(names.isdisjoint({"importance", "score", "threshold", "emotional_delta", "sentiment"}))

    def test_retrieval_requires_structured_scope_and_person_boundary(self) -> None:
        with self.assertRaises(ValueError):
            MemoryQuery("query-1", (MemoryKind.AUTOBIOGRAPHICAL,))
        with self.assertRaises(ValueError):
            MemoryQuery("query-2", (MemoryKind.RELATIONSHIP,), memory_ids=("memory-1",))
        with self.assertRaises(ValueError):
            MemoryQuery("query-3", (MemoryKind.FACTUAL,), person_id="friedl")
        with self.assertRaises(ValueError):
            MemoryQuery(
                "query-4",
                (MemoryKind.RELATIONSHIP, MemoryKind.AUTOBIOGRAPHICAL),
                person_id="friedl",
            )


class SQLiteMemoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "memories.sqlite3"
        self.store = SQLiteMemoryStore(self.path)

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def test_restart_recovers_content_provenance_and_retention(self) -> None:
        retention = NOW + timedelta(days=90)
        saved = self.store.create(proposal("memory-1"), retention)
        self.store.close()
        self.store = SQLiteMemoryStore(self.path)
        recovered = self.store.load("memory-1")
        self.assertEqual(recovered, saved)
        self.assertEqual(recovered.current.source_references, ("conversation:turn-7", "goal:goal-2:evidence-4"))

    def test_correction_appends_revision_without_rewriting_history(self) -> None:
        saved = self.store.create(proposal("memory-1"), NOW + timedelta(days=90))
        corrected = self.store.correct(
            "memory-1",
            MemoryCorrection(
                "I revised my view after later evidence.", "Later evidence corrected the first account.",
                ("conversation:turn-11",), NOW + timedelta(hours=1), "I became more careful about early conclusions.",
            ),
            saved.revision,
        )
        self.assertEqual(corrected.revision, 2)
        self.assertEqual(corrected.revisions[0], saved.revisions[0])
        self.assertEqual(corrected.current.reason, "Later evidence corrected the first account.")
        with self.assertRaises(MemoryRevisionConflict):
            self.store.correct("memory-1", MemoryCorrection("x", "y", ("turn-12",), NOW, "z"), saved.revision)

    def test_categories_are_distinct_and_relationship_queries_are_person_scoped(self) -> None:
        self.store.create(proposal("fact-1", MemoryKind.FACTUAL), NOW + timedelta(days=30))
        self.store.create(proposal("friedl-1", MemoryKind.RELATIONSHIP, person_id="friedl"), NOW + timedelta(days=30))
        self.store.create(proposal("other-1", MemoryKind.RELATIONSHIP, person_id="other"), NOW + timedelta(days=30))
        self.assertEqual([item.memory_id for item in self.store.list_memories(MemoryKind.FACTUAL)], ["fact-1"])
        self.assertEqual([item.memory_id for item in self.store.list_memories(MemoryKind.RELATIONSHIP, person_id="friedl")], ["friedl-1"])
        with self.assertRaises(ValueError):
            self.store.list_memories(MemoryKind.RELATIONSHIP)

    def test_supersession_requires_a_real_prior_memory(self) -> None:
        with self.assertRaises(SupersededMemoryNotFound):
            self.store.create(proposal("memory-2", supersedes="missing"), NOW + timedelta(days=90))
        self.store.create(proposal("memory-1"), NOW + timedelta(days=90))
        evolved = self.store.create(proposal("memory-2", supersedes="memory-1"), NOW + timedelta(days=90))
        self.assertEqual(evolved.supersedes_memory_id, "memory-1")
        self.store.create(
            proposal("friedl-relationship", MemoryKind.RELATIONSHIP, person_id="friedl"),
            NOW + timedelta(days=90),
        )
        with self.assertRaises(InvalidMemorySupersession):
            self.store.create(
                proposal(
                    "other-relationship", MemoryKind.RELATIONSHIP,
                    person_id="other", supersedes="friedl-relationship",
                ),
                NOW + timedelta(days=90),
            )

    def test_identical_core_retry_is_idempotent_but_conflicting_identity_fails(self) -> None:
        original = proposal("memory-1")
        retention = NOW + timedelta(days=90)
        first = self.store.remember(original, retention)
        self.assertEqual(self.store.remember(original, retention), first)
        conflict = MemoryProposal(
            "memory-1", MemoryKind.AUTOBIOGRAPHICAL, "different reflection",
            ("conversation:turn-7",), NOW, meaning="different meaning",
        )
        with self.assertRaises(MemoryIdentityConflict):
            self.store.remember(conflict, retention)

    def test_retrieval_filters_without_keywords_scores_or_cross_person_leakage(self) -> None:
        retention = NOW + timedelta(days=90)
        self.store.create(proposal("friedl-1", MemoryKind.RELATIONSHIP, person_id="friedl"), retention)
        self.store.create(proposal("other-1", MemoryKind.RELATIONSHIP, person_id="other"), retention)
        self.store.create(proposal("old-view"), retention)
        self.store.create(proposal("new-view", supersedes="old-view"), retention)
        self.store.create(proposal("revived-old"), retention)
        self.store.create(proposal("expired-new", supersedes="revived-old"), NOW)
        self.store.create(proposal("expired"), NOW)

        relationship = self.store.retrieve(
            MemoryQuery("query-1", (MemoryKind.RELATIONSHIP,), person_id="friedl"),
            NOW,
        )
        self.assertEqual([item.memory_id for item in relationship], ["friedl-1"])
        current = self.store.retrieve(
            MemoryQuery(
                "query-2", (MemoryKind.AUTOBIOGRAPHICAL,),
                memory_ids=("old-view", "new-view"),
                source_references=("conversation:turn-7",),
                source_match=MemorySourceMatch.ALL,
            ),
            NOW,
        )
        self.assertEqual([item.memory_id for item in current], ["new-view"])
        revived = self.store.retrieve(
            MemoryQuery(
                "query-revived", (MemoryKind.AUTOBIOGRAPHICAL,),
                memory_ids=("revived-old", "expired-new"),
            ),
            NOW,
        )
        self.assertEqual([item.memory_id for item in revived], ["revived-old"])
        with_history = self.store.retrieve(
            MemoryQuery(
                "query-3", (MemoryKind.AUTOBIOGRAPHICAL,),
                memory_ids=("old-view", "new-view"), include_superseded=True,
            ),
            NOW,
        )
        self.assertEqual([item.memory_id for item in with_history], ["new-view", "old-view"])

    def test_inspection_delete_and_retention_purge(self) -> None:
        doomed = self.store.create(proposal("delete-me"), NOW + timedelta(days=1))
        self.store.create(proposal("expired"), NOW, )
        self.store.delete("delete-me", doomed.revision)
        with self.assertRaises(MemoryNotFound):
            self.store.load("delete-me")
        self.assertEqual(self.store.purge_expired(NOW), ("expired",))
        with self.assertRaises(MemoryNotFound):
            self.store.load("expired")


if __name__ == "__main__":
    unittest.main()
