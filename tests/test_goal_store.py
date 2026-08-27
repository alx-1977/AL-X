from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    Approval, ApprovalLifecycle, ApprovalScope, CapabilityAttempt, CapabilityAttemptDisposition, CapabilityCall, CapabilityResult, CapabilityResultState,
    ConversationOrigin, ConversationTurn, Evidence, GoalState, GoalStatus, GoalStopReason,
    Objective, ProgressRecord, Referent, SuccessCriterion, WorkItem,
)
from alx.goals import (  # noqa: E402
    GoalAlreadyExists, GoalNotFound, GoalRevisionConflict, SQLiteGoalStore, UnsupportedSchema,
)
from alx.goals.store import _goal_to_data  # noqa: E402


NOW = datetime(2026, 8, 27, 9, 30, tzinfo=UTC)


def turn(identifier: str, origin: ConversationOrigin = ConversationOrigin.TYPED) -> ConversationTurn:
    return ConversationTurn("conversation-1", identifier, origin, f"turn {identifier}", NOW)


def state(identifier: str = "goal-1", **changes: object) -> GoalState:
    values: dict[str, object] = {
        "goal_id": identifier,
        "objective": Objective("turn-1", "objective summary"),
        "success_criteria": (SuccessCriterion("criterion-1", "criterion summary"),),
        "context": {"nested": {"items": ("one", 2, True)}},
        "referents": (Referent("referent-1", {"kind": "record"}),),
        "decisions": (ProgressRecord("decision-1", "decision", ("evidence-1",)),),
        "corrections": (ProgressRecord("correction-1", "correction", ("evidence-1",)),),
        "progress": (ProgressRecord("progress-1", "progress", ("evidence-1",)),),
        "attempts": (
            CapabilityAttempt(CapabilityCall("call-1", "capability-1"), CapabilityAttemptDisposition.EXECUTED, True, CapabilityResult("call-1", "capability-1", CapabilityResultState.PARTIAL, {"available": 1}, {"code": "limited"}, ("evidence-1",))),
            CapabilityAttempt(CapabilityCall("call-2", "capability-2"), CapabilityAttemptDisposition.EXECUTED, True, CapabilityResult("call-2", "capability-2", CapabilityResultState.FAILED, failure={"code": "unavailable"})),
        ),
        "blockers": (WorkItem("blocker-1", "blocker"),),
        "outstanding_work": (WorkItem("work-1", "outstanding"),),
        "evidence": (Evidence("evidence-1", "observation", {"source": "source-1"}, ("criterion-1",)),),
        "approvals": (Approval("approval-1", ApprovalScope("capability-3", {"record_id": "record-1"}), ApprovalLifecycle.GRANTED, NOW + timedelta(hours=1)),),
    }
    values.update(changes)
    return GoalState(**values)  # type: ignore[arg-type]


class GoalStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "goals.sqlite3"
        self.store = SQLiteGoalStore(self.path)

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def test_round_trips_every_goal_field(self) -> None:
        original = state()
        retention = (NOW + timedelta(days=1)).astimezone(timezone(timedelta(hours=2)))
        saved = self.store.create(original, (), retention)
        loaded = self.store.load(original.goal_id)
        self.assertEqual(loaded.state, original)
        self.assertEqual(loaded.revision, saved.revision)
        self.assertEqual(loaded.retention_until, saved.retention_until)

    def test_turns_round_trip_in_insertion_order(self) -> None:
        turns = (turn("turn-1"), turn("turn-2", ConversationOrigin.SPEECH_TRANSCRIPT), turn("turn-3"))
        self.store.create(state(), turns, NOW + timedelta(days=1))
        self.assertEqual(self.store.load("goal-1").turns, turns)

    def test_close_and_reopen_recovers_unfinished_goal_and_turns(self) -> None:
        original = state()
        turns = (turn("turn-1"), turn("turn-2", ConversationOrigin.SPEECH_TRANSCRIPT))
        self.store.create(original, turns, NOW + timedelta(days=1))
        self.store.close()
        self.store = SQLiteGoalStore(self.path)
        recovered = self.store.load("goal-1")
        self.assertEqual(recovered.state.objective, original.objective)
        self.assertEqual(recovered.state.corrections, original.corrections)
        self.assertEqual(recovered.state.progress, original.progress)
        self.assertEqual(recovered.state.evidence, original.evidence)
        self.assertEqual(recovered.state.blockers, original.blockers)
        self.assertEqual(recovered.state.outstanding_work, original.outstanding_work)
        self.assertEqual(recovered.turns, turns)

    def test_revision_conflict_prevents_lost_update(self) -> None:
        saved = self.store.create(state(), (), NOW + timedelta(days=1))
        stale_writer = SQLiteGoalStore(self.path)
        try:
            stale_snapshot = stale_writer.load("goal-1")
            updated = self.store.replace(state(), (), NOW + timedelta(days=2), saved.revision)
            with self.assertRaises(GoalRevisionConflict):
                stale_writer.replace(state(), (), NOW + timedelta(days=3), stale_snapshot.revision)
            self.assertEqual(self.store.load("goal-1").revision, updated.revision)
        finally:
            stale_writer.close()

    def test_non_default_status_and_stop_reason_round_trip(self) -> None:
        waiting = state(
            status=GoalStatus.AWAITING_INPUT,
            stop_reason=GoalStopReason.REQUIRED_INPUT,
        )
        self.store.create(waiting, (), NOW + timedelta(days=1))
        loaded = self.store.load("goal-1").state
        self.assertEqual(loaded.status, GoalStatus.AWAITING_INPUT)
        self.assertEqual(loaded.stop_reason, GoalStopReason.REQUIRED_INPUT)

    def test_failed_compound_replace_rolls_back_goal_and_turns(self) -> None:
        original_turns = (turn("turn-1"),)
        saved = self.store.create(state(), original_turns, NOW + timedelta(days=1))
        duplicate_turns = (turn("turn-2"), turn("turn-2", ConversationOrigin.SPEECH_TRANSCRIPT))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.replace(state(), duplicate_turns, NOW + timedelta(days=2), saved.revision)
        recovered = self.store.load("goal-1")
        self.assertEqual(recovered.revision, saved.revision)
        self.assertEqual(recovered.turns, original_turns)

    def test_inspection_listing_and_delete_removes_turns(self) -> None:
        first = self.store.create(state("goal-a"), (turn("turn-a"),), NOW + timedelta(days=1))
        self.store.create(state("goal-b"), (), NOW + timedelta(days=1))
        self.assertEqual([item.state.goal_id for item in self.store.list_goals()], ["goal-a", "goal-b"])
        self.store.delete("goal-a", first.revision)
        with self.assertRaises(GoalNotFound):
            self.store.load("goal-a")
        recreated = self.store.create(state("goal-a"), (turn("turn-a"),), NOW + timedelta(days=1))
        self.assertEqual(self.store.load("goal-a").turns, (turn("turn-a"),))
        self.assertEqual(recreated.revision, 1)
        self.assertEqual([item.state.goal_id for item in self.store.list_goals()], ["goal-a", "goal-b"])

    def test_retention_purge_uses_supplied_time(self) -> None:
        self.store.create(state("expired"), (), NOW)
        self.store.create(state("retained"), (), NOW + timedelta(seconds=1))
        self.assertEqual(self.store.purge_expired(NOW), ("expired",))
        with self.assertRaises(GoalNotFound):
            self.store.load("expired")
        self.assertEqual(self.store.load("retained").state.goal_id, "retained")

    def test_create_rejects_duplicate_and_naive_retention(self) -> None:
        self.store.create(state(), (), NOW + timedelta(days=1))
        with self.assertRaises(GoalAlreadyExists):
            self.store.create(state(), (), NOW + timedelta(days=1))
        with self.assertRaises(ValueError):
            self.store.create(state("goal-2"), (), datetime(2026, 8, 27))

    def test_future_schema_is_rejected(self) -> None:
        self.store.close()
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA user_version = 3")
        connection.commit()
        connection.close()
        with self.assertRaises(UnsupportedSchema):
            SQLiteGoalStore(self.path)

    def test_v1_json_migrates_to_truthful_legacy_attempt(self) -> None:
        saved = self.store.create(state(), (), NOW + timedelta(days=1))
        payload = _goal_to_data(saved.state)
        result = saved.state.attempts[0].result
        payload.pop("attempts")
        payload["completed_actions"] = [[result.call_id, result.capability_id, result.state.value, dict(result.values), dict(result.failure), list(result.evidence_refs)]]
        self.store._connection.execute("UPDATE goals SET state_json = ?", (json.dumps(payload),))
        self.store._connection.execute("PRAGMA user_version = 1")
        self.store._connection.commit(); self.store.close(); self.store = SQLiteGoalStore(self.path)
        legacy = self.store.load("goal-1").state.attempts[0]
        self.assertEqual(legacy.disposition, CapabilityAttemptDisposition.LEGACY)
        self.assertIsNone(legacy.call); self.assertIsNone(legacy.implementation_invoked); self.assertEqual(legacy.reason_code, "legacy_v1")
        self.assertEqual(legacy.result, result)
        snapshot = self.store.load("goal-1")
        self.store.replace(snapshot.state, snapshot.turns, snapshot.retention_until, snapshot.revision)
        self.store.close(); self.store = SQLiteGoalStore(self.path)
        self.assertEqual(self.store.load("goal-1").state.attempts[0].call, None)


if __name__ == "__main__":
    unittest.main()
