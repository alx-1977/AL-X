from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    Evidence, GoalState, MemoryKind, MemoryProposal, Objective,
    SuccessCriterion,
)
from alx.goals import (  # noqa: E402
    GoalAlreadyExists, GoalNotFound, GoalRevisionConflict, SQLiteGoalStore,
    UnsupportedSchema,
)
from alx.goals.store import SCHEMA_VERSION, _goal_to_data  # noqa: E402

NOW = datetime(2026, 8, 27, 9, 30, tzinfo=UTC)


def state(identifier: str = "goal-1") -> GoalState:
    return GoalState(
        identifier,
        Objective("turn:turn-1", "objective"),
        (SuccessCriterion("criterion-1", "success"),),
        evidence=(Evidence("evidence-1", "fact", supports=("criterion-1",),
                           source_references=("turn:turn-1",)),),
    )


class GoalStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "goals.sqlite3"
        self.store = SQLiteGoalStore(self.path)

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def test_goal_is_attached_to_conversation_without_owning_turns(self) -> None:
        saved = self.store.create(state(), "conversation-1", NOW + timedelta(days=1))
        self.assertEqual(saved.conversation_id, "conversation-1")
        self.assertFalse(hasattr(saved, "turns"))
        self.store.close()
        self.store = SQLiteGoalStore(self.path)
        self.assertEqual(self.store.load("goal-1"), saved)

    def test_revision_conflict_and_duplicate_are_rejected(self) -> None:
        saved = self.store.create(state(), "conversation-1", NOW + timedelta(days=1))
        stale = SQLiteGoalStore(self.path)
        try:
            self.store.replace(state(), NOW + timedelta(days=2), saved.revision)
            with self.assertRaises(GoalRevisionConflict):
                stale.replace(state(), NOW + timedelta(days=3), saved.revision)
            with self.assertRaises(GoalAlreadyExists):
                self.store.create(state(), "conversation-1", NOW + timedelta(days=1))
        finally:
            stale.close()

    def test_memory_batch_is_atomic_with_goal_revision(self) -> None:
        saved = self.store.create(state(), "conversation-1", NOW + timedelta(days=1))
        proposal = MemoryProposal(
            "memory-1", MemoryKind.FACTUAL, "fact", ("turn:turn-1",), NOW,
        )
        updated = self.store.replace_with_memory_batch(
            saved.state, saved.retention_until, saved.revision, (proposal,)
        )
        self.assertEqual(
            self.store.pending_memory_batches("goal-1")[0].goal_revision,
            updated.revision,
        )
        self.store.acknowledge_memory_batch("goal-1", updated.revision)
        self.assertEqual(self.store.pending_memory_batches("goal-1"), ())

    def test_delete_and_retention_are_goal_scoped_only(self) -> None:
        saved = self.store.create(state("expired"), "conversation-1", NOW)
        self.store.create(state("retained"), "conversation-1", NOW + timedelta(days=1))
        self.assertEqual(self.store.purge_expired(NOW), ("expired",))
        with self.assertRaises(GoalNotFound):
            self.store.load(saved.state.goal_id)
        self.assertEqual(self.store.load("retained").conversation_id, "conversation-1")

    def test_future_schema_is_rejected(self) -> None:
        self.store.close()
        connection = sqlite3.connect(self.path)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        connection.commit()
        connection.close()
        with self.assertRaises(UnsupportedSchema):
            SQLiteGoalStore(self.path)
        self.store = SQLiteGoalStore(Path(self.directory.name) / "replacement.sqlite3")

    def test_legacy_json_preserves_attempt_compatibility(self) -> None:
        saved = self.store.create(state(), "conversation-1", NOW + timedelta(days=1))
        payload = _goal_to_data(saved.state)
        payload["evidence"][0] = payload["evidence"][0][:4]
        self.store._connection.execute(
            "UPDATE goals SET state_json = ?", (json.dumps(payload),)
        )
        self.store._connection.commit()
        loaded = self.store.load("goal-1")
        self.assertEqual(loaded.state.evidence[0].source_references, ())


if __name__ == "__main__":
    unittest.main()
