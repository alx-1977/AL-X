"""Phase 0a PR B: durable action timing and readable reasoning spend."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.capabilities import CapabilityBroker, CapabilityRegistry
from alx.contracts import (
    CapabilityCall, CapabilityDefinition, CapabilityResult, CapabilityResultState,
    GoalState, Objective, SideEffect, StructuredSchema, SuccessCriterion, ValueKind,
)
from alx.core.model_reasoner import _attempt_payload, _state_payload
from alx.goals import SQLiteGoalStore
from alx.goals.store import _attempts, _goal_to_data
from alx.observability.self_record import SQLiteTraceRecord
from alx.observability.usage import SQLiteUsageRecorder
from alx.safety import AuthorityContext, AuthorityPolicy, SafetyGate
from alx.tools.goal_history import READ_OWN_GOALS, build_goal_history_executors
from alx.tools.self_diagnosis import (
    READ_OWN_TRACE, TRACE_DEFINITION, build_self_diagnosis_executors,
)


class ActionTimingTests(unittest.TestCase):
    def test_all_dispatched_outcomes_keep_timing_result_and_trace_across_reopen(self):
        for outcome in ("succeeded", "failed", "raised", "refused", "rejected"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                now = datetime.now(UTC)
                trace = SQLiteTraceRecord(root / "trace.sqlite3")
                schema = StructuredSchema(ValueKind.OBJECT)
                definition = CapabilityDefinition(
                    "observe", "Read structured facts", schema, schema, SideEffect.NONE,
                    ("unavailable", "operation_refused"),
                )
                facts = {"tool": "existing-tool", "model": "existing-model",
                         "session_report": "Existing report"}
                def execute(arguments):
                    if outcome == "raised":
                        raise RuntimeError("test failure")
                    return CapabilityResult(
                        "call", "observe",
                        CapabilityResultState.SUCCEEDED if outcome == "succeeded"
                        else CapabilityResultState.FAILED,
                        facts if outcome == "succeeded" else {},
                        failure=None if outcome == "succeeded" else {
                            "code": "operation_refused" if outcome == "refused" else "unavailable"},
                    )
                broker = CapabilityBroker(
                    CapabilityRegistry((definition,)),
                    SafetyGate({"observe": AuthorityPolicy(frozenset({"read"}))}),
                    {"observe": execute}, trace=lambda event: trace.record(event.values()),
                )
                with patch("alx.capabilities.broker.monotonic", side_effect=(10.0, 12.5)):
                    attempt = broker.dispatch(
                        CapabilityCall("call", "observe", {}),
                        AuthorityContext("friedl", frozenset() if outcome == "rejected"
                                         else frozenset({"read"}), now),
                    )
                self.assertEqual(attempt.implementation_invoked, outcome != "rejected")
                self.assertEqual(attempt.duration_seconds, 2.5)
                self.assertGreaterEqual(attempt.started_at, now)
                state = GoalState(
                    "goal", Objective("turn:1", "Observe facts"),
                    (SuccessCriterion("observed", "Facts observed"),), attempts=(attempt,),
                )
                store = SQLiteGoalStore(root / "goals.sqlite3")
                store.create(state, "conversation", now + timedelta(days=30))
                store.close()
                store = SQLiteGoalStore(root / "goals.sqlite3")
                try:
                    restored = store.load("goal").state
                    self.assertEqual(restored.attempts, (attempt,))
                    payload = _state_payload(restored)["attempts"][0]
                    self.assertEqual(payload["started_at"], attempt.started_at.isoformat())
                    self.assertEqual(payload["duration_seconds"], 2.5)
                    self.assertEqual(_attempt_payload(attempt)["duration_seconds"], 2.5)
                    result = build_goal_history_executors(store, lambda: "read")[READ_OWN_GOALS](
                        {"goal_id": "goal"})
                    stored_attempt = result.values["goals"][0]["state"]["attempts"][0]
                    self.assertEqual(stored_attempt[12], attempt.started_at.isoformat())
                    self.assertEqual(stored_attempt[13], 2.5)
                    if outcome == "succeeded":
                        self.assertEqual(dict(restored.attempts[0].result.values), facts)
                    if outcome == "raised":
                        self.assertEqual(restored.attempts[0].result.failure["code"], "executor_error")
                    # Legacy rows keep unknown timing, without fabricated dates.
                    old = _goal_to_data(state)
                    old["attempts"][0] = old["attempts"][0][:12]
                    self.assertIsNone(_attempts(old)[0].started_at)
                    self.assertIsNone(_attempts(old)[0].duration_seconds)
                finally:
                    store.close()
                    trace.close()
                trace = SQLiteTraceRecord(root / "trace.sqlite3")
                try:
                    result = build_self_diagnosis_executors(
                        trace, lambda *args: ([], False), lambda: "trace",
                        clock=lambda: datetime.now(UTC) + timedelta(seconds=1),
                    )[READ_OWN_TRACE]({})
                    entries = result.values["entries"]
                    self.assertEqual(
                        [item["status"] for item in entries],
                        ["refused"] if outcome == "rejected" else
                        ["started", "completed" if outcome == "succeeded" else "failed"],
                    )
                    self.assertEqual(entries[-1]["duration_ms"], 2500)
                    self.assertIn("at", entries[0])
                finally:
                    trace.close()

    def test_safety_refusal_is_timed_without_invocation(self):
        schema = StructuredSchema(ValueKind.OBJECT)
        definition = CapabilityDefinition("observe", "Read facts", schema, schema, SideEffect.NONE)
        broker = CapabilityBroker(CapabilityRegistry((definition,)), SafetyGate({}), {})
        attempt = broker.dispatch(CapabilityCall("call", "observe", {}),
                                  AuthorityContext("friedl", frozenset(), datetime.now(UTC)))
        self.assertFalse(attempt.implementation_invoked)
        self.assertIsNotNone(attempt.started_at)
        self.assertGreaterEqual(attempt.duration_seconds, 0)


class ReasoningUsageVisibilityTests(unittest.TestCase):
    def test_calls_and_full_window_totals_survive_reopen_and_are_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.sqlite3"
            start = datetime.now(UTC) - timedelta(seconds=1)
            recorder = SQLiteUsageRecorder(path)
            values = {"code": "reasoning.completed", "provider": "test", "model": "model",
                      "input_tokens": 100, "output_tokens": 20, "cached_tokens": 80,
                      "cache_write_tokens": 10, "cost_usd": 0.25}
            recorder.record("conversation", values)
            recorder.record("conversation", {**values, "cost_usd": 0.5})
            recorder.record("unknown", {"code": "reasoning.failed"})
            recorder.record("zero", {**values, "input_tokens": 0, "output_tokens": 0,
                                     "cached_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0})
            reopened = SQLiteUsageRecorder(path)
            end = datetime.now(UTC) + timedelta(seconds=1)
            record = reopened.between(start, end)
            self.assertEqual(len(record["calls"]), 4)
            for name in ("input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens", "cost_usd"):
                self.assertEqual(record["calls"][0][name], values[name])
                self.assertIsNone(record["calls"][2][name])
                self.assertEqual(record["calls"][3][name], 0)
            self.assertEqual(record["totals"]["cost_usd"], 0.75)
            self.assertEqual(record["totals"]["input_tokens"], 200)
            self.assertEqual(reopened.task("conversation")["cost_usd"], 0.75)
            trace = SQLiteTraceRecord(Path(directory) / "trace.sqlite3")
            try:
                result = build_self_diagnosis_executors(
                    trace, lambda *args: ([], False), lambda: "read",
                    read_usage=reopened.between,
                )[READ_OWN_TRACE]({"from": start.isoformat(), "to": end.isoformat(), "limit": 1})
                self.assertTrue(TRACE_DEFINITION.output_schema.accepts(result.values))
                usage = result.values["reasoning_usage"]
                self.assertEqual(len(usage["calls"]), 1)
                self.assertTrue(usage["truncated"])
                self.assertEqual(usage["totals"]["calls"], 4)
                self.assertEqual(usage["totals"]["cost_usd"], 0.75)
            finally:
                trace.close()

    def test_reservation_updates_one_record_and_preserves_measured_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.sqlite3"
            recorder = SQLiteUsageRecorder(path)
            start = datetime.now(UTC) - timedelta(seconds=1)
            recorder.record("task", {"code": "research.reserved", "reservation_id": "one",
                                      "reserved_usd": 1})
            recorder.record("task", {"code": "research.completed", "reservation_id": "one",
                                      "input_tokens": 3, "output_tokens": 1, "cost_usd": 0.1})
            usage = SQLiteUsageRecorder(path).between(
                start, datetime.now(UTC) + timedelta(seconds=1))
            self.assertEqual(usage["totals"]["calls"], 1)
            self.assertEqual(usage["totals"]["cost_usd"], 0.1)
            self.assertEqual(usage["calls"][0]["input_tokens"], 3)
            self.assertIsNone(usage["calls"][0]["cached_tokens"])

    def test_existing_database_migrates_without_inventing_known_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.sqlite3"
            recorder = SQLiteUsageRecorder(path)
            start = datetime.now(UTC) - timedelta(seconds=1)
            recorder.record("task", {"code": "reasoning.completed", "input_tokens": 3})
            with sqlite3.connect(path) as database:
                database.execute("ALTER TABLE reasoning_calls DROP COLUMN reported_fields")
            reopened = SQLiteUsageRecorder(path)
            usage = reopened.between(start, datetime.now(UTC) + timedelta(seconds=1))
            self.assertEqual(usage["calls"][0]["input_tokens"], 3)
            self.assertIsNone(usage["calls"][0]["cost_usd"])
            self.assertIsNone(usage["totals"]["cost_usd"])
