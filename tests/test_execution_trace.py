"""The operator can watch AL/X work, live, and one question gets one answer.

On 2026-10-05 a two-minute turn showed "Authoritative Core reasoning in
progress", then nothing, then a burst of identical "Reasoning completed" lines
all stamped with the same second, reporting "undefined tier", "input 2" and
"total 0". Then two replies were spoken for one question, the older one last.

These tests pin the causes rather than the symptoms:

- diagnostics were buffered until the Core turn returned;
- nothing recorded why each reasoning call was made;
- Anthropic usage was read in another provider's layout, and absent figures
  were rendered as zero;
- an autonomous reply admitted while a person turn waited for the Core was
  queued behind it and spoken after that turn's answer.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from alx.bootstrap.autonomous import AutonomousCognitionRunner  # noqa: E402
from alx.capabilities import CapabilityBroker, CapabilityRegistry  # noqa: E402
from alx.contracts import (  # noqa: E402
    AgentDecision, AudioChunk, CapabilityAttempt, CapabilityAttemptDisposition,
    CapabilityCall, CapabilityDefinition, CapabilityResult, CapabilityResultState,
    CognitionOrigin, ConversationOrigin, ConversationSnapshot, ConversationTurn,
    GoalState, GoalStatus, ModelMessage, ModelRequest, ModelRole, Objective,
    ReasoningPurpose, ResponseDelivery, SideEffect, StructuredSchema,
    SuccessCriterion, TraceStatus, TraceSubsystem, ValueKind, normalise_usage,
    usage_telemetry,
)
from alx.core import CoreAgent, CoreState  # noqa: E402
from alx.core.loop import CoreOutcome, _StepMarks, reasoning_purpose  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402
from alx.interfaces import VoiceDiagnosticFeed, VoiceEventKind, VoiceSession  # noqa: E402
from alx.interfaces.server import LiveVoiceServer  # noqa: E402
from alx.providers.claude_subscription import ClaudeSubscriptionReasoningModel  # noqa: E402
from alx.safety.gate import AuthorityContext, AuthorityPolicy, SafetyGate  # noqa: E402

NOW = datetime(2026, 10, 5, 8, 14, 38, tzinfo=UTC)
RETENTION = NOW + timedelta(days=30)
SCHEMA = StructuredSchema(ValueKind.OBJECT)
INSPECT = CapabilityDefinition(
    "inspect", "Inspect structured material", SCHEMA, SCHEMA, SideEffect.NONE,
)


def _answer(response: str = "One answer.") -> CoreOutcome:
    return CoreOutcome(
        state=CoreState.RESPONDED,
        snapshot=SimpleNamespace(state=SimpleNamespace(status=GoalStatus.ACTIVE)),
        response=response,
    )


class _Transcriber:
    """Open for the life of the exchange, as a live session is."""

    async def transcribe(self, audio):
        await asyncio.Future()
        yield  # pragma: no cover - never reached


class _Synthesizer:
    def __init__(self) -> None:
        self.responses: list[str] = []

    async def synthesize(self, response, correlation_id=None):
        self.responses.append(response)
        yield AudioChunk("tts", 0, b"spoken", "audio/mpeg")
        yield AudioChunk("tts", 1, b"", "audio/mpeg", final=True)


async def _silence():
    await asyncio.Future()
    yield  # pragma: no cover - never reached


async def _collect_until(exchange, stop, limit: float = 5.0) -> list:
    """Collect events until `stop(events)` says enough, then close the stream."""
    events: list = []

    async def run() -> None:
        async for event in exchange:
            events.append(event)
            if stop(events):
                return

    await asyncio.wait_for(run(), timeout=limit)
    await exchange.aclose()
    return events


def _trace_labels(events) -> list[str]:
    return [
        event.diagnostic["label"] for event in events
        if event.kind is VoiceEventKind.DIAGNOSTIC
        and event.diagnostic.get("code") == "trace"
    ]


class LiveProgressTests(unittest.IsolatedAsyncioTestCase):
    """Requirement 1: progress is visible while the work is happening."""

    async def test_progress_arrives_while_the_core_turn_is_still_running(self) -> None:
        feed = VoiceDiagnosticFeed()
        seen = threading.Event()

        class SlowGateway:
            def receive_conversation_turn(self, turn, step_budget, retention_until):
                feed.publish(turn.conversation_id, {
                    "code": "trace", "subsystem": "core", "status": "started",
                    "label": "Interpreting request",
                })
                # The turn cannot finish until the console has shown the
                # progress line. A buffer flushed at the end would deadlock
                # here, and the bounded wait would fail the test.
                if not seen.wait(timeout=3):
                    raise AssertionError("progress was not delivered live")
                return _answer()

        typed: asyncio.Queue[str] = asyncio.Queue()
        await typed.put("What is the current status?")
        session = VoiceSession(
            SlowGateway(), _Transcriber(), _Synthesizer(), "friedl", 8, 30,
            clock=lambda: NOW, diagnostics=feed,
        )

        def stop(events) -> bool:
            if "Interpreting request" in _trace_labels(events):
                seen.set()
            return any(event.kind is VoiceEventKind.TEXT for event in events)

        events = await _collect_until(
            session.exchange("conversation-1", _silence(), typed=typed), stop,
        )
        kinds = [event.kind for event in events]
        progress = next(
            index for index, event in enumerate(events)
            if event.kind is VoiceEventKind.DIAGNOSTIC
            and event.diagnostic.get("label") == "Interpreting request"
        )
        self.assertLess(progress, kinds.index(VoiceEventKind.TEXT))

    async def test_synthesis_stages_stream_before_the_first_audio_arrives(self) -> None:
        feed = VoiceDiagnosticFeed()
        connected = asyncio.Event()

        class SlowSynthesizer:
            async def synthesize(self, response, correlation_id=None):
                feed.publish(correlation_id, {"code": "tts.request_sent", "elapsed_ms": 0})
                # The provider has not answered yet. The stage must already
                # be visible; the audio waits until the console has shown it.
                await asyncio.wait_for(connected.wait(), timeout=3)
                feed.publish(correlation_id, {"code": "tts.first_audio_byte", "elapsed_ms": 900})
                yield AudioChunk("tts", 0, b"spoken", "audio/mpeg")
                yield AudioChunk("tts", 1, b"", "audio/mpeg", final=True)

        class Gateway:
            def receive_conversation_turn(self, turn, step_budget, retention_until):
                return _answer()

        typed: asyncio.Queue[str] = asyncio.Queue()
        await typed.put("Hello")
        session = VoiceSession(
            Gateway(), _Transcriber(), SlowSynthesizer(), "friedl", 8, 30,
            clock=lambda: NOW, diagnostics=feed,
        )

        def stop(events) -> bool:
            codes = [e.diagnostic.get("code") for e in events
                     if e.kind is VoiceEventKind.DIAGNOSTIC]
            if "tts.request_sent" in codes:
                connected.set()
            return any(e.kind is VoiceEventKind.LISTENING for e in events)

        events = await _collect_until(
            session.exchange("conversation-1", _silence(), typed=typed), stop,
        )
        order = [
            e.diagnostic["code"] if e.kind is VoiceEventKind.DIAGNOSTIC else e.kind.value
            for e in events
            if e.kind in (VoiceEventKind.DIAGNOSTIC, VoiceEventKind.AUDIO)
        ]
        self.assertEqual(order[:3], ["tts.request_sent", "tts.first_audio_byte", "audio"])

    async def test_a_turn_behind_a_busy_core_says_that_it_is_waiting(self) -> None:
        feed = VoiceDiagnosticFeed()
        lock = asyncio.Lock()
        await lock.acquire()

        class Gateway:
            def receive_conversation_turn(self, turn, step_budget, retention_until):
                return _answer()

        typed: asyncio.Queue[str] = asyncio.Queue()
        await typed.put("Status?")
        session = VoiceSession(
            Gateway(), _Transcriber(), None, "friedl", 8, 30,
            clock=lambda: NOW, diagnostics=feed, core_turn_lock=lock,
        )

        def stop(events) -> bool:
            if "Waiting for background work to finish" in _trace_labels(events):
                if lock.locked():
                    lock.release()
            return any(event.kind is VoiceEventKind.TEXT for event in events)

        events = await _collect_until(
            session.exchange("conversation-1", _silence(), typed=typed), stop,
        )
        self.assertIn("Waiting for background work to finish", _trace_labels(events))

    async def test_background_work_is_streamed_while_idle_and_marked(self) -> None:
        feed = VoiceDiagnosticFeed()
        session = VoiceSession(
            SimpleNamespace(), _Transcriber(), None, "friedl", 8, 30,
            clock=lambda: NOW, diagnostics=feed,
        )
        exchange = session.exchange("conversation-1", _silence())
        first = asyncio.ensure_future(exchange.__anext__())
        await asyncio.sleep(0)
        threading.Thread(target=lambda: feed.publish("mail-thread:other", {
            "code": "trace", "subsystem": "core", "status": "started",
            "label": "External event received",
        })).start()
        event = await asyncio.wait_for(first, timeout=3)
        await exchange.aclose()
        self.assertIs(event.kind, VoiceEventKind.DIAGNOSTIC)
        self.assertTrue(event.diagnostic["background"])
        # The other thread's identifier names its sender; it is never shown.
        self.assertNotIn("mail-thread:other", json.dumps(event.diagnostic))


class TimestampTests(unittest.TestCase):
    """Requirement 2: a line's time is when it happened, in emission order."""

    def test_events_are_stamped_at_publication_in_order(self) -> None:
        moments = iter(NOW + timedelta(seconds=offset) for offset in (0, 11, 23))
        feed = VoiceDiagnosticFeed(clock=lambda: next(moments))
        received: list[dict] = []
        feed.subscribe(lambda _owner, event: received.append(event))
        for code in ("first", "second", "third"):
            feed.publish("conversation-1", {"code": code})

        self.assertEqual([event["seq"] for event in received], [1, 2, 3])
        self.assertEqual(
            [event["at"] for event in received],
            [(NOW + timedelta(seconds=offset)).isoformat(timespec="milliseconds")
             for offset in (0, 11, 23)],
        )

    def test_the_console_renders_the_server_time_not_its_arrival(self) -> None:
        script = (ROOT / "src/alx/interfaces/assets/app.js").read_text(encoding="utf-8")
        self.assertIn("eventDate(options.at)", script)
        self.assertNotIn("function clockTime()", script)


class ReasoningPurposeTests(unittest.TestCase):
    """Requirement 3: every reasoning call carries why it was made."""

    @staticmethod
    def _marks(**changes) -> _StepMarks:
        values = dict(dispatches=0, last_capability=None, refused=0, last_refusal=None,
                      goal_refusals=0, memories=0, notices=0, goal_id=None,
                      answering_plan=None)
        values.update(changes)
        return _StepMarks(**values)

    def test_the_first_call_is_named_by_what_started_the_turn(self) -> None:
        expected = {
            CognitionOrigin.PERSON_TURN: ReasoningPurpose.INTERPRETING_REQUEST,
            CognitionOrigin.EXTERNAL_EVENT: ReasoningPurpose.ASSESSING_EVENT,
            CognitionOrigin.WORK_COMPLETED: ReasoningPurpose.REVIEWING_COMPLETED_WORK,
            CognitionOrigin.SELF_REQUESTED: ReasoningPurpose.REVISITING_FOLLOW_UP,
        }
        for origin, purpose in expected.items():
            with self.subTest(origin=origin):
                self.assertEqual(
                    reasoning_purpose(0, origin, False, self._marks(), None)[0], purpose,
                )
        self.assertEqual(
            reasoning_purpose(0, CognitionOrigin.WORK_COMPLETED, True, self._marks(), None)[0],
            ReasoningPurpose.EVALUATING_PLAN,
        )

    def test_later_calls_are_named_by_what_the_previous_step_added(self) -> None:
        before = self._marks()
        cases = (
            (self._marks(dispatches=1, last_capability="inspect"),
             (ReasoningPurpose.REVIEWING_RESULT, "inspect")),
            (self._marks(refused=1, last_refusal="decision_rejected"),
             (ReasoningPurpose.CORRECTING_DECISION, None)),
            (self._marks(refused=1, last_refusal="call_id_reused"),
             (ReasoningPurpose.RECONSIDERING_REFUSAL, "call_id_reused")),
            (self._marks(goal_id="goal-1"), (ReasoningPurpose.EVALUATING_GOAL_STATE, None)),
            (self._marks(memories=2), (ReasoningPurpose.REVIEWING_MEMORIES, None)),
            (self._marks(notices=1), (ReasoningPurpose.CONTINUING_WORK, None)),
            (self._marks(), (ReasoningPurpose.DECIDING_NEXT_STEP, None)),
        )
        for marks, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    reasoning_purpose(1, CognitionOrigin.PERSON_TURN, False, marks, before),
                    expected,
                )

    def test_the_core_traces_each_call_and_hands_its_purpose_to_the_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteGoalStore(Path(directory) / "goals.sqlite3")
            self.addCleanup(store.close)
            store.create(GoalState(
                goal_id="goal-1", objective=Objective("turn:turn-1", "Do the work"),
                success_criteria=(SuccessCriterion("criterion-1", "verified"),),
            ), "conversation-1", RETENTION)
            contexts: list = []
            decisions = [
                AgentDecision(goal_id="goal-1",
                              call=CapabilityCall("call-1", "inspect", {})),
                AgentDecision(goal_id="goal-1", response="Done."),
            ]

            class Reasoner:
                def decide(self, context):
                    contexts.append(context)
                    return decisions.pop(0)

            def dispatch(call, state):
                return CapabilityAttempt(
                    call, CapabilityAttemptDisposition.EXECUTED, True,
                    CapabilityResult(call.call_id, call.capability_id,
                                     CapabilityResultState.SUCCEEDED, {}),
                )

            traced: list = []
            agent = CoreAgent(store, Reasoner(), dispatch, (INSPECT,),
                              clock=lambda: NOW, trace=traced.append)
            turn = ConversationTurn("conversation-1", "turn-1", ConversationOrigin.TYPED,
                                    "Inspect it", NOW, "friedl")
            outcome = agent.process(
                ConversationSnapshot("conversation-1", (turn,), 1, RETENTION), RETENTION, 8,
            )

        self.assertIs(outcome.state, CoreState.RESPONDED)
        self.assertEqual(
            [context.purpose for context in contexts],
            [ReasoningPurpose.INTERPRETING_REQUEST, ReasoningPurpose.REVIEWING_RESULT],
        )
        core = [item for item in traced if item.subsystem is TraceSubsystem.CORE]
        self.assertEqual(
            [(item.label, item.reference) for item in core],
            [("Interpreting request", None), ("Reviewing result", "inspect")],
        )
        self.assertTrue(all(item.conversation_id == "conversation-1" for item in core))

    def test_the_purpose_reaches_provider_telemetry(self) -> None:
        telemetry: list = []
        envelope = {
            "type": "result", "subtype": "success", "is_error": False,
            "structured_output": {"finding": "ok"},
            "usage": {"input_tokens": 2, "cache_read_input_tokens": 40_000,
                      "cache_creation_input_tokens": 512, "output_tokens": 795,
                      "service_tier": "standard"},
            "duration_api_ms": 12_000,
            "modelUsage": {"claude-opus-5-5": {"outputTokens": 795}},
        }

        def runner(command, **_kwargs):
            return subprocess.CompletedProcess(command, 0, json.dumps(envelope), "")

        model = ClaudeSubscriptionReasoningModel(
            "claude-opus-5-5", 60, runner=runner, environment={"PATH": "/bin"},
            telemetry_sink=lambda key, values: telemetry.append(values),
        )
        model.complete(ModelRequest(
            (ModelMessage(ModelRole.SYSTEM, "laws"), ModelMessage(ModelRole.USER, "turn")),
            "answer",
            {"type": "object", "properties": {"finding": {"type": "string"}},
             "required": ["finding"]},
            affinity_key="conversation-1",
            purpose=ReasoningPurpose.REVIEWING_RESULT.value,
        ))
        (values,) = telemetry
        self.assertEqual(values["purpose"], "reviewing_result")
        self.assertEqual(values["service_tier"], "standard")
        self.assertEqual(values["api_duration_ms"], 12_000)
        self.assertEqual(values["input_tokens"], 40_514)
        self.assertEqual(values["cached_tokens"], 40_000)
        self.assertEqual(values["total_tokens"], 41_309)
        # Anthropic reports no reasoning breakdown; none is published as zero.
        self.assertNotIn("reasoning_tokens", values)
        # The CLI reports no streaming timings, so none are invented.
        for absent in ("first_event_ms", "first_content_ms", "answer_generation_ms"):
            self.assertNotIn(absent, values)

    def test_an_unknown_purpose_is_refused_at_the_contract(self) -> None:
        with self.assertRaises(ValueError):
            ModelRequest((ModelMessage(ModelRole.USER, "x"),), "answer", {},
                         purpose="thinking about lunch")


class MissingTelemetryTests(unittest.TestCase):
    """Requirement 4: an absent figure is reported as absent, never as zero."""

    def test_anthropic_usage_counts_cache_reads_as_input(self) -> None:
        usage = normalise_usage({
            "input_tokens": 2, "cache_read_input_tokens": 30_100,
            "cache_creation_input_tokens": 410, "output_tokens": 717,
        })
        self.assertEqual(usage["input_tokens"], 30_512)
        self.assertEqual(usage["cached_tokens"], 30_100)
        self.assertEqual(usage["cache_write_tokens"], 410)
        self.assertEqual(usage["total_tokens"], 31_229)

    def test_a_reported_total_that_counts_the_cache_inside_is_respected(self) -> None:
        usage = normalise_usage({
            "input_tokens": 1_000, "cache_read_input_tokens": 800,
            "cache_creation_input_tokens": 0, "output_tokens": 100,
            "total_tokens": 1_100,
        })
        self.assertEqual(usage["input_tokens"], 1_000)
        self.assertEqual(usage["cached_tokens"], 800)

    def test_a_breakdown_the_provider_never_reported_is_not_published(self) -> None:
        reported = {"input_tokens": 2, "cache_read_input_tokens": 6_013,
                    "cache_creation_input_tokens": 27_983, "output_tokens": 327}
        values = usage_telemetry(normalise_usage(reported), reported)
        self.assertNotIn("reasoning_tokens", values)
        self.assertEqual(values["cached_tokens"], 6_013)
        openai = {"input_tokens": 100, "output_tokens": 50,
                  "output_tokens_details": {"reasoning_tokens": 0}}
        self.assertEqual(
            usage_telemetry(normalise_usage(openai), openai)["reasoning_tokens"], 0)

    def test_an_unmeasured_call_publishes_no_counts(self) -> None:
        self.assertEqual(usage_telemetry(normalise_usage(None)), {"usage_measured": False})
        measured = usage_telemetry(normalise_usage({"input_tokens": 10, "output_tokens": 5}))
        self.assertTrue(measured["usage_measured"])
        self.assertEqual(measured["total_tokens"], 15)

    def test_the_console_names_absent_figures_instead_of_zero(self) -> None:
        script = (ROOT / "src/alx/interfaces/assets/app.js").read_text(encoding="utf-8")
        self.assertIn('"not reported"', script)
        self.assertIn("message.usage_measured !== true", script)
        # The tier is shown only when a provider reported one.
        self.assertIn("message.service_tier ?", script)
        for invented in ("input_tokens ?? 0", "total_tokens ?? 0", "?? 0) / 1000"):
            self.assertNotIn(invented, script)


class OneTurnOneAnswerTests(unittest.IsolatedAsyncioTestCase):
    """Requirements 5 and 6: one question, one accepted answer, one synthesis."""

    async def test_a_reply_admitted_while_a_person_turn_waits_is_not_spoken_after_it(self) -> None:
        synthesizer = _Synthesizer()
        server = LiveVoiceServer.__new__(LiveVoiceServer)
        server._delivery_queues = {}
        server._typed_queues = {}
        server._delivery_loop = asyncio.get_running_loop()
        results: list[ResponseDelivery] = []

        class Gateway:
            def receive_conversation_turn(self, turn, step_budget, retention_until):
                # The observed case: a background turn finishing while the
                # person turn waits, its reply offered for speech.
                results.append(server.deliver("conversation-1", "Older background reply."))
                return _answer("The one answer.")

        session = VoiceSession(
            Gateway(), _Transcriber(), synthesizer, "friedl", 8, 30, clock=lambda: NOW,
        )
        server._session = session
        deliveries: asyncio.Queue[str] = asyncio.Queue()
        server._delivery_queues["conversation-1"] = [deliveries]
        typed: asyncio.Queue[str] = asyncio.Queue()
        await typed.put("What is the current status?")

        events = await _collect_until(
            session.exchange("conversation-1", _silence(), deliveries, typed),
            lambda events: sum(e.kind is VoiceEventKind.LISTENING for e in events) >= 1,
        )

        self.assertEqual(results, [ResponseDelivery.UNDELIVERABLE])
        self.assertEqual(
            [event.text for event in events if event.kind is VoiceEventKind.TEXT],
            ["The one answer."],
        )
        self.assertEqual(synthesizer.responses, ["The one answer."])
        self.assertTrue(deliveries.empty())

    async def test_the_voice_is_free_again_once_the_turn_has_answered(self) -> None:
        session = VoiceSession(
            SimpleNamespace(receive_conversation_turn=lambda *_: _answer()),
            _Transcriber(), None, "friedl", 8, 30, clock=lambda: NOW,
        )
        typed: asyncio.Queue[str] = asyncio.Queue()
        await typed.put("Hello")
        await _collect_until(
            session.exchange("conversation-1", _silence(), typed=typed),
            lambda events: any(e.kind is VoiceEventKind.LISTENING for e in events),
        )
        self.assertTrue(session.admits_unprompted_speech("conversation-1"))

    async def test_a_reply_admitted_before_the_question_is_spoken_before_it(self) -> None:
        synthesizer = _Synthesizer()
        order: list[str] = []

        class Gateway:
            def receive_conversation_turn(self, turn, step_budget, retention_until):
                order.append("person turn reasoned")
                return _answer("The answer.")

        session = VoiceSession(
            Gateway(), _Transcriber(), synthesizer, "friedl", 8, 30, clock=lambda: NOW,
        )
        deliveries: asyncio.Queue[str] = asyncio.Queue()
        typed: asyncio.Queue[str] = asyncio.Queue()
        self.assertTrue(session.admits_unprompted_speech("conversation-1"))
        deliveries.put_nowait("Earlier reply.")
        typed.put_nowait("Question")

        events = await _collect_until(
            session.exchange("conversation-1", _silence(), deliveries, typed),
            lambda events: sum(e.kind is VoiceEventKind.TEXT for e in events) >= 2
            and events[-1].kind is VoiceEventKind.LISTENING,
        )
        self.assertEqual(
            [event.text for event in events if event.kind is VoiceEventKind.TEXT],
            ["Earlier reply.", "The answer."],
        )
        self.assertEqual(synthesizer.responses, ["Earlier reply.", "The answer."])

    def test_a_held_reply_is_recorded_undelivered_for_the_core_to_judge(self) -> None:
        recorded: dict = {}

        class Ledger:
            def record_outcome(self, opportunity_id, state, **_values):
                recorded["state"] = state

            def mark_response_undelivered(self, opportunity_id):
                recorded["undelivered"] = opportunity_id

            def record_reserved(self, *_args):
                pass

        class Source:
            def claim(self, _opportunity):
                return True

            def mark_honoured(self, _opportunity):
                recorded["honoured"] = True

        class Gateway:
            def receive_cognition_opportunity(self, *_args):
                return SimpleNamespace(state=CoreState.RESPONDED, response="Reply.")

        traced: list = []
        runner = AutonomousCognitionRunner(
            Source(), Ledger(), Gateway(), 8, 30,
            response_transport=SimpleNamespace(
                deliver=lambda *_: ResponseDelivery.UNDELIVERABLE),
            clock=lambda: NOW, trace=traced.append,
        )
        opportunity = SimpleNamespace(
            opportunity_id="occasion-1", conversation_id="conversation-1",
            origin=CognitionOrigin.WORK_COMPLETED,
        )
        self.assertTrue(runner.run_one(opportunity))
        self.assertEqual(recorded["undelivered"], "occasion-1")
        self.assertEqual(
            [(item.label, item.status) for item in traced],
            [("Completed work received", TraceStatus.STARTED),
             ("Response recorded as undelivered", TraceStatus.INFO)],
        )


class CapabilityActivityTests(unittest.TestCase):
    """Requirement 3: real execution activity is visible by subsystem."""

    def _broker(self, definition, executor, traced, allowed=True):
        registry = CapabilityRegistry()
        registry.register(definition)
        policy = AuthorityPolicy(frozenset({"use"}))
        return CapabilityBroker(
            registry, SafetyGate({definition.capability_id: policy}),
            {definition.capability_id: executor}, trace=traced.append,
            subsystems={definition.capability_id: TraceSubsystem.GITHUB},
        ), AuthorityContext("friedl", frozenset({"use"} if allowed else set()), NOW)

    def test_a_dispatch_is_traced_with_its_declared_identifier(self) -> None:
        definition = CapabilityDefinition(
            "read_pull_request_checks", "Read checks",
            StructuredSchema(ValueKind.OBJECT, {
                "pull_request_number": StructuredSchema(ValueKind.INTEGER),
                "note": StructuredSchema(ValueKind.STRING),
            }),
            SCHEMA, SideEffect.NONE, trace_fields=("pull_request_number",),
        )
        traced: list = []
        broker, authority = self._broker(definition, lambda arguments: CapabilityResult(
            "call-1", "read_pull_request_checks", CapabilityResultState.SUCCEEDED, {},
        ), traced)
        broker.dispatch(CapabilityCall(
            "call-1", "read_pull_request_checks",
            {"pull_request_number": 39, "note": "private wording"},
        ), authority)

        self.assertEqual(
            [(item.subsystem, item.status, item.label, item.reference) for item in traced],
            [(TraceSubsystem.GITHUB, TraceStatus.STARTED, "Read pull request checks", "#39"),
             (TraceSubsystem.GITHUB, TraceStatus.COMPLETED, "Read pull request checks", "#39")],
        )
        self.assertIsNotNone(traced[-1].duration_ms)
        self.assertNotIn("private wording", repr(traced))

    def test_a_refusal_and_a_failing_trace_sink_change_nothing(self) -> None:
        traced: list = []
        broker, authority = self._broker(INSPECT, lambda arguments: None, traced, allowed=False)
        attempt = broker.dispatch(CapabilityCall("call-1", "inspect", {}), authority)
        self.assertIs(attempt.disposition, CapabilityAttemptDisposition.REJECTED)
        self.assertEqual([item.status for item in traced], [TraceStatus.REFUSED])

        def broken(_event):
            raise RuntimeError("console gone")

        registry = CapabilityRegistry()
        registry.register(INSPECT)
        quiet = CapabilityBroker(
            registry, SafetyGate({"inspect": AuthorityPolicy()}),
            {"inspect": lambda arguments: CapabilityResult(
                "call-2", "inspect", CapabilityResultState.SUCCEEDED, {})},
            trace=broken,
        )
        attempt = quiet.dispatch(CapabilityCall("call-2", "inspect", {}),
                                 AuthorityContext("friedl", frozenset(), NOW))
        self.assertIs(attempt.disposition, CapabilityAttemptDisposition.EXECUTED)


class SinglePathTests(unittest.TestCase):
    """Requirement 7: the buffer that held telemetry until turn end is gone."""

    def test_the_end_of_turn_buffer_no_longer_exists(self) -> None:
        source = "\n".join(
            path.read_text(encoding="utf-8") for path in (ROOT / "src/alx").rglob("*.py")
        )
        self.assertNotIn("VoiceDiagnosticBuffer", source)
        self.assertNotIn("diagnostics.drain(", source)


if __name__ == "__main__":
    unittest.main()
