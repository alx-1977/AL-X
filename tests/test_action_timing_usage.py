"""Phase 0a PR B: durable action timing and readable reasoning spend."""

from datetime import UTC, datetime, timedelta
from itertools import product
from pathlib import Path
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.capabilities import CapabilityBroker, CapabilityRegistry
from alx.contracts import (
    Cognition, ModelCompletion, ResearchQuestion, SpecialistQuestion, SpecialistError,
    CapabilityCall, CapabilityDefinition, CapabilityResult, CapabilityResultState,
    GoalState, Objective, SideEffect, StructuredSchema, SuccessCriterion, ValueKind,
)
from alx.core.model_reasoner import _attempt_payload, _state_payload
from alx.goals import SQLiteGoalStore
from alx.goals.store import _attempts, _goal_to_data
from alx.observability.self_record import SQLiteTraceRecord
from alx.observability.usage import SQLiteUsageRecorder
from alx.observability.pricing import ConfiguredPricingWorstCase
from alx.observability.research_budget import ResearchBudget, SQLiteResearchLedger
from alx.specialists import ResearchSpecialist, ResearchTierModel
from alx.providers import OpenAIReasoningModel, XAIReasoningModel, ClaudeSubscriptionResearchModel
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
    def test_usage_failure_keeps_successful_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = SQLiteTraceRecord(Path(directory) / "trace.db")
            try:
                now = datetime.now(UTC)
                trace.record({"code": "trace", "at": now.isoformat(), "subsystem": "core",
                              "status": "completed", "label": "Observed facts"})

                def unavailable(*args):
                    raise sqlite3.OperationalError("usage store locked")

                result = build_self_diagnosis_executors(
                    trace, lambda *args: ([], False), lambda: "read",
                    clock=lambda: now + timedelta(seconds=1), read_usage=unavailable,
                )[READ_OWN_TRACE]({})
                self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
                self.assertEqual(len(result.values["entries"]), 1)
                self.assertEqual(result.values["entries"][0]["label"], "Observed facts")
                self.assertFalse(result.values["truncated"])
                self.assertEqual(result.values["reasoning_usage"], {
                    "failure": {"code": "unavailable", "reason": "OperationalError"}})
                self.assertTrue(TRACE_DEFINITION.output_schema.accepts(result.values))
            finally:
                trace.close()

    def test_production_research_events_preserve_measurement_provenance(self):
        class Model:
            supports_bounded_research = True

            def complete(self, request):
                if usage is None:
                    raise RuntimeError("transport unavailable")
                return ModelCompletion("openai", "gpt-5.4-nano", {"finding": "fact"}, usage)

        cases = (
            (None, False, False),  # transport failure
            ({}, False, False),  # pricing cannot determine cost
            ({"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
              "reasoning_tokens": 0}, False, False),  # normalized unknown usage
            ({"input_tokens": 2, "output_tokens": 0, "cached_tokens": 0,
              "reasoning_tokens": 0, "cache_write_tokens": 0}, True, True),
            ({"input_tokens": 2, "output_tokens": 0, "cached_tokens": 0,
              "reasoning_tokens": 0}, True, False),
        )
        for usage, measured, priced in cases:
            with self.subTest(usage=usage, priced=priced), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                start = datetime.now(UTC) - timedelta(seconds=1)
                recorder = SQLiteUsageRecorder(root / "usage.db")
                events = []

                def record(task, event):
                    events.append(event)
                    recorder.record(task, event)

                prices = ConfiguredPricingWorstCase()
                if measured and not priced:
                    prices.cost_usd = lambda *args: None
                specialist = ResearchSpecialist(
                    {Cognition.SURVEY: ResearchTierModel("openai", "gpt-5.4-nano", Model())},
                    SQLiteResearchLedger(root / "budget.db", ResearchBudget(1, 0.09)),
                    prices, 4000, 1000, 0.09, telemetry_sink=record,
                )
                question = ResearchQuestion(SpecialistQuestion(
                    "question", "Read the material", "material",
                    {"type": "object", "properties": {"finding": {"type": "string"}}},
                ))
                if usage is None:
                    with self.assertRaises(SpecialistError):
                        specialist.answer(question)
                else:
                    specialist.answer(question)
                result = SQLiteUsageRecorder(root / "usage.db").between(
                    start, datetime.now(UTC) + timedelta(seconds=1))
                self.assertEqual(result["totals"]["calls"], 1)
                for name in ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens"):
                    expected = usage[name] if measured else None
                    self.assertEqual(result["calls"][0][name], expected)
                    self.assertEqual(result["totals"][name], expected)
                if priced:
                    self.assertIsNotNone(result["calls"][0]["cost_usd"])
                else:
                    self.assertIsNone(result["calls"][0]["cost_usd"])
                    self.assertIsNone(result["totals"]["cost_usd"])
                    self.assertEqual(recorder.task("question")["cost_usd"], 0)
                self.assertIs(events[-1]["usage_measured"], measured)
                self.assertIs(events[-1]["cost_measured"], priced)

    def test_partial_provider_reports_survive_the_research_lifecycle(self):
        for provider, details in product(
            ("openai", "xai", "claude_subscription"),
            ({}, {"cached_tokens": 0}, {"cache_write_tokens": 1}, {"reasoning_tokens": 0}),
        ):
            with self.subTest(provider=provider, details=details), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                start = datetime.now(UTC) - timedelta(seconds=1)
                recorder = SQLiteUsageRecorder(root / "usage.db")
                events = []

                def record(task, event):
                    events.append(event)
                    recorder.record(task, event)

                def respond(request):
                    return httpx.Response(200, json={
                        "model": "gpt-5.4-nano",
                        "output": [{"type": "message", "content": [
                            {"type": "output_text", "text": '{"finding":"fact"}'}]}],
                        "choices": [{"message": {"content": '{"finding":"fact"}'}}],
                        "usage": {"input_tokens": 100, "output_tokens": 2,
                                  "input_tokens_details": details,
                                  "output_tokens_details": details},
                    })

                with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                    model = "gpt-5.4-nano"
                    prices = ConfiguredPricingWorstCase()
                    if provider == "claude_subscription":
                        model = "claude-haiku-5-5"

                        def run(command, **kwargs):
                            return subprocess.CompletedProcess(command, 0, json.dumps({
                                "subtype": "success", "structured_output": {"finding": "fact"},
                                "usage": {"input_tokens": 100, "output_tokens": 2, **details},
                            }), "")

                        transport = ClaudeSubscriptionResearchModel(model, 30, runner=run,
                                                                    telemetry_sink=record)
                    else:
                        adapter = OpenAIReasoningModel if provider == "openai" else XAIReasoningModel
                        transport = adapter(model, "key", "https://provider.invalid", 30,
                                            client=client, streaming=False, telemetry_sink=record)
                        if provider == "xai":
                            # Exercise the adapter without adding a production price.
                            prices.is_priced = lambda *args: True
                            prices.worst_case_usd = lambda *args: 0.01
                            prices.cost_usd = lambda *args: 0.001
                    specialist = ResearchSpecialist(
                        {Cognition.SURVEY: ResearchTierModel(provider, model, transport)},
                        SQLiteResearchLedger(root / "budget.db", ResearchBudget(1, 0.09)),
                        prices, 4000, 1000, 0.09, telemetry_sink=record,
                    )
                    specialist.answer(ResearchQuestion(SpecialistQuestion(
                        "question", "Read the material", "material",
                        {"type": "object", "properties": {"finding": {"type": "string"}}},
                    )))
                result = SQLiteUsageRecorder(root / "usage.db").between(
                    start, datetime.now(UTC) + timedelta(seconds=1))
                self.assertEqual(result["totals"]["calls"], 1)
                expected = {"input_tokens": 100, "output_tokens": 2, **details}
                for name in SQLiteUsageRecorder.USAGE_FIELDS[:-1]:
                    self.assertEqual(result["calls"][0][name], expected.get(name), name)
                    self.assertEqual(result["totals"][name], expected.get(name), name)
                self.assertEqual(set(events[-1]["reported_fields"]), set(expected) | {"cost_usd"})

    def test_whole_event_flags_and_version_one_rows_are_not_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.db"
            recorder = SQLiteUsageRecorder(path)
            start = datetime.now(UTC) - timedelta(seconds=1)
            event = {"code": "research.completed", "kind": "research",
                     "usage_measured": True, "cost_measured": True,
                     "input_tokens": 2, "output_tokens": 0, "cached_tokens": 0,
                     "reasoning_tokens": 0, "cost_usd": 0.09}
            recorder.record("legacy-event", event)
            recorder.record("legacy-row", event)
            with sqlite3.connect(path) as database:
                database.execute("UPDATE reasoning_calls SET research_measurement_version = 1, "
                                 "reported_fields = ? WHERE task_id = 'legacy-row'",
                                 ('["input_tokens", "output_tokens", "cost_usd"]',))
            result = SQLiteUsageRecorder(path).between(start, datetime.now(UTC) + timedelta(seconds=1))
            for name in SQLiteUsageRecorder.USAGE_FIELDS:
                self.assertIsNone(result["totals"][name], name)
                for call in result["calls"]:
                    self.assertIsNone(call[name], name)

    def test_legacy_research_records_and_events_do_not_establish_measurements(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.db"
            recorder = SQLiteUsageRecorder(path)
            start = datetime.now(UTC) - timedelta(seconds=1)
            for task in ("legacy-event", "legacy-row", "legacy-null"):
                recorder.record(task, {
                    "code": "research.completed", "kind": "research", "input_tokens": 0,
                    "output_tokens": 0, "cost_usd": 0.09,
                })
            with sqlite3.connect(path) as database:
                database.execute("ALTER TABLE reasoning_calls DROP COLUMN research_measurement_version")
                database.execute("UPDATE reasoning_calls SET reported_fields = ? WHERE task_id = ?",
                                 ('["input_tokens", "output_tokens", "cost_usd"]', "legacy-row"))
                database.execute("UPDATE reasoning_calls SET reported_fields = NULL WHERE task_id = ?",
                                 ("legacy-null",))
            usage = SQLiteUsageRecorder(path).between(start, datetime.now(UTC) + timedelta(seconds=1))
            self.assertEqual(usage["totals"]["calls"], 3)
            for name in ("input_tokens", "output_tokens", "cost_usd"):
                self.assertIsNone(usage["totals"][name])
                for call in usage["calls"]:
                    self.assertIsNone(call[name])

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
                                      "usage_measured": True, "cost_measured": True,
                                      "reported_fields": ["input_tokens", "output_tokens", "cost_usd"],
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
