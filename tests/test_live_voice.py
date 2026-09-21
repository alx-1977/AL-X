from __future__ import annotations

import asyncio
from collections import deque
import tempfile
import unittest
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.bootstrap.live_voice import (  # noqa: E402
    load_environment, migrate_legacy_conversations,
)
from alx.contracts import (  # noqa: E402
    AudioChunk,
    BackgroundEvent,
    ConversationOrigin,
    ConversationTurn,
    GoalState,
    GoalStatus,
    Objective,
    SuccessCriterion,
    TranscriptionEvent,
    TranscriptionState,
)
from alx.contracts.coding import CodingTelemetry  # noqa: E402
from alx.interfaces import (  # noqa: E402
    VoiceActivityStatus,
    VoiceDiagnosticBuffer,
    VoiceEventKind,
    VoiceSession,
)
from alx.interfaces.live_voice import VoiceEvent  # noqa: E402
from alx.interfaces.server import LiveVoiceServer  # noqa: E402
from alx.core import CoreState
from alx.core.loop import CoreOutcome  # noqa: E402
from alx.conversation import SQLiteConversationStore  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402
from alx.goals.store import _goal_to_data  # noqa: E402


NOW = datetime(2026, 8, 28, 9, 30, tzinfo=UTC)


class FakeTranscriber:
    def __init__(self, events):
        self.events = events
        self.received = []

    async def transcribe(self, chunks):
        self.received = [chunk async for chunk in chunks]
        for event in self.events:
            yield event


class FakeSynthesizer:
    def __init__(self):
        self.responses = []

    async def synthesize(self, response, correlation_id=None):
        self.responses.append(response)
        yield AudioChunk("tts", 0, b"spoken", "audio/mpeg")
        yield AudioChunk("tts", 1, b"", "audio/mpeg", final=True)


class OpenTranscriber:
    """Stays open, as a live session does, so typed input is reachable.

    An exhausted transcriber ends the exchange before anything typed is read,
    which makes an empty transcriber the wrong fixture for a typed-input test.
    """

    async def transcribe(self, audio):
        await asyncio.Future()
        yield  # pragma: no cover - never reached


class FakeGateway:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []
        self.thread_ids = []

    def receive_conversation_turn(self, turn, step_budget, retention_until):
        self.thread_ids.append(threading.get_ident())
        self.calls.append((turn, step_budget, retention_until))
        return next(self.outcomes)


def outcome(
    status,
    response="authoritative response",
    reason=None,
    core_state=CoreState.RESPONDED,
):
    # The real contract, not a look-alike: a SimpleNamespace silently lacks
    # any field the loop later adds, so these tests kept passing while the
    # transport read something that did not exist.
    return CoreOutcome(
        state=core_state,
        snapshot=SimpleNamespace(state=SimpleNamespace(status=status)),
        response=response,
        reason=reason,
    )


def transcription(identifier, state, content):
    return TranscriptionEvent("stt", identifier, state, content, NOW)


async def incoming_audio():
    yield AudioChunk("mic", 0, b"pcm", "audio/pcm", 16000)


class VoiceDiagnosticBufferTests(unittest.TestCase):
    def test_task_status_keeps_only_the_latest_event_per_task(self) -> None:
        diagnostics = VoiceDiagnosticBuffer()
        diagnostics.publish(
            "conversation-1",
            {"code": "task.status", "task_id": "review-1", "state": "requested"},
        )
        diagnostics.publish(
            "conversation-1",
            {"code": "task.status", "task_id": "review-2", "state": "requested"},
        )
        diagnostics.publish(
            "conversation-1",
            {"code": "task.status", "task_id": "review-1", "state": "completed"},
        )

        events = diagnostics.drain("conversation-1")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1]["task_id"], "review-1")
        self.assertEqual(events[-1]["state"], "completed")

    def test_each_dormant_conversation_has_a_hard_event_limit(self) -> None:
        diagnostics = VoiceDiagnosticBuffer(max_events_per_conversation=2)
        diagnostics.publish("conversation-1", {"code": "first"})
        diagnostics.publish("conversation-1", {"code": "second"})
        diagnostics.publish("conversation-1", {"code": "third"})

        self.assertEqual(
            [event["code"] for event in diagnostics.drain("conversation-1")],
            ["second", "third"],
        )


class CodingTelemetryPresentationTests(unittest.TestCase):
    def _telemetry(self, **changes):
        values = {
            "job_id": "case-7", "phase": "execution", "started_at": NOW - timedelta(minutes=4),
            "phase_started_at": NOW - timedelta(minutes=2),
            "last_activity_at": NOW - timedelta(minutes=3), "provider": "grok",
        }
        values.update(changes)
        return CodingTelemetry(**values)

    def test_the_snapshot_says_where_the_job_is_working(self) -> None:
        """The canonical checkout stays on main, so the panel must say where.

        The Coding Agent always knew the worktree and branch; they reached the
        person only in the final outcome, after the work was over. A running
        job could therefore be visibly active and give no way to find its
        files short of `git worktree list`.
        """
        activity = VoiceActivityStatus()
        activity.publish_coding(self._telemetry(
            worktree="/tmp/wt/case-7", branch="fix/thing",
        ))
        snapshot = activity.coding_snapshot(NOW)
        self.assertEqual(snapshot["worktree"], "/tmp/wt/case-7")
        self.assertEqual(snapshot["branch"], "fix/thing")

    def test_a_job_without_an_allocation_reports_no_worktree(self) -> None:
        """Empty is a real state, and must not be filled in with a guess."""
        activity = VoiceActivityStatus()
        activity.publish_coding(self._telemetry())
        snapshot = activity.coding_snapshot(NOW)
        self.assertEqual(snapshot["worktree"], "")
        self.assertEqual(snapshot["branch"], "")

    def test_a_terminal_observation_still_carries_its_worktree(self) -> None:
        """The frontend needs it to name the retained directory as it clears."""
        activity = VoiceActivityStatus()
        activity.publish_coding(self._telemetry(
            terminal=True, outcome="failed",
            worktree="/tmp/wt/case-7", branch="fix/thing",
        ))
        snapshot = activity.coding_snapshot(NOW)
        self.assertTrue(snapshot["terminal"])
        self.assertEqual(snapshot["worktree"], "/tmp/wt/case-7")

    def test_snapshot_uses_runtime_timestamps_not_console_text(self) -> None:
        activity = VoiceActivityStatus()
        activity.publish_coding(self._telemetry(last_activity_at=NOW - timedelta(seconds=7)))
        snapshot = activity.coding_snapshot(NOW)
        self.assertEqual(snapshot["elapsed_seconds"], 240)
        self.assertEqual(snapshot["last_activity_seconds"], 7)
        self.assertFalse(snapshot["stalled"])

    def test_silent_running_owner_remains_healthy_beyond_stall_threshold(self) -> None:
        async def running_owner_snapshot():
            activity = VoiceActivityStatus()
            owner = asyncio.create_task(asyncio.Event().wait())
            activity.set_coding_owner_alive(lambda: not owner.done())
            activity.publish_coding(self._telemetry(in_flight=True))
            snapshot = activity.coding_snapshot(NOW)
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)
            return snapshot

        self.assertFalse(asyncio.run(running_owner_snapshot())["stalled"])

    def test_terminated_execution_owner_without_terminal_telemetry_is_unresponsive(self) -> None:
        async def terminated_owner_snapshot():
            activity = VoiceActivityStatus()
            owner = asyncio.create_task(asyncio.sleep(0))
            await owner
            activity.set_coding_owner_alive(lambda: not owner.done())
            activity.publish_coding(self._telemetry(in_flight=True))
            return activity.coding_snapshot(NOW)

        snapshot = asyncio.run(terminated_owner_snapshot())
        self.assertTrue(snapshot["stalled"])
        self.assertTrue(snapshot["unresponsive"])

    def test_non_in_flight_inactivity_still_stalls(self) -> None:
        activity = VoiceActivityStatus()
        activity.set_coding_owner_alive(lambda: True)
        activity.publish_coding(self._telemetry(in_flight=False))
        self.assertTrue(activity.coding_snapshot(NOW)["stalled"])

    def test_terminal_result_remains_authoritative(self) -> None:
        activity = VoiceActivityStatus()
        activity.publish_coding(self._telemetry(phase="failed", terminal=True, outcome="failed"))
        snapshot = activity.coding_snapshot(NOW)
        self.assertTrue(snapshot["terminal"])
        self.assertEqual(snapshot["outcome"], "failed")
        self.assertFalse(snapshot["stalled"])

    def test_later_task_cannot_revive_stale_coding_owner(self) -> None:
        async def stale_owner_snapshot():
            activity = VoiceActivityStatus()
            owner_a = asyncio.create_task(asyncio.sleep(0))
            await owner_a
            activity.set_coding_owner_alive(lambda: not owner_a.done())
            activity.publish_coding(self._telemetry(in_flight=True))

            owner_b = asyncio.create_task(asyncio.Event().wait())
            activity.set_coding_owner_alive(lambda: not owner_b.done())
            snapshot = activity.coding_snapshot(NOW)
            owner_b.cancel()
            await asyncio.gather(owner_b, return_exceptions=True)
            return snapshot

        snapshot = asyncio.run(stale_owner_snapshot())
        self.assertFalse(snapshot["owner_alive"])
        self.assertTrue(snapshot["unresponsive"])
        self.assertTrue(snapshot["stalled"])


class VoiceSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_coding_transitions_are_emitted_in_order(self) -> None:
        activity = VoiceActivityStatus()
        release = threading.Event()

        class TransitionGateway(FakeGateway):
            def receive_conversation_turn(self, turn, step_budget, retention_until):
                for phase, transition in (
                    ("plan", "PLAN completed"),
                    ("execution", "EXECUTION started"),
                    ("test", "TEST started"),
                ):
                    activity.publish_coding(CodingTelemetry(
                        job_id="case-ordered", phase=phase, started_at=NOW,
                        phase_started_at=NOW, last_activity_at=NOW,
                        in_flight=phase == "execution", transition=transition,
                    ))
                if not release.wait(timeout=1):
                    raise RuntimeError("test did not release the Core worker")
                return super().receive_conversation_turn(
                    turn, step_budget, retention_until
                )

        session = VoiceSession(
            TransitionGateway((outcome(GoalStatus.ACTIVE),)),
            FakeTranscriber((transcription("one", TranscriptionState.FINAL, "Hi"),)),
            FakeSynthesizer(), "friedl", 8, 3650,
            clock=lambda: NOW, identifier_factory=lambda: "turn-1", activity=activity,
        )
        iterator = session.exchange("conversation-1", incoming_audio())
        self.assertIs((await iterator.__anext__()).kind, VoiceEventKind.THINKING)
        events = [await asyncio.wait_for(iterator.__anext__(), timeout=0.2) for _ in range(3)]
        self.assertEqual(
            [event.diagnostic["transition"] for event in events],
            ["PLAN completed", "EXECUTION started", "TEST started"],
        )
        self.assertEqual(activity.coding_snapshot(NOW)["phase"], "test")
        release.set()
        [event async for event in iterator]

    async def test_activity_is_forwarded_while_the_same_core_turn_is_running(self) -> None:
        """The terminal sees explicit worker activity before Core returns."""
        activity = VoiceActivityStatus()
        release = threading.Event()

        class CodingGateway(FakeGateway):
            def receive_conversation_turn(self, turn, step_budget, retention_until):
                activity.set("coding")
                if not release.wait(timeout=1):
                    raise RuntimeError("test did not release the Core worker")
                return super().receive_conversation_turn(
                    turn, step_budget, retention_until
                )

        session = VoiceSession(
            CodingGateway((outcome(GoalStatus.ACTIVE),)),
            FakeTranscriber((transcription("one", TranscriptionState.FINAL, "Hi"),)),
            FakeSynthesizer(), "friedl", 8, 3650,
            clock=lambda: NOW, identifier_factory=lambda: "turn-1",
            activity=activity,
        )
        iterator = session.exchange("conversation-1", incoming_audio())
        self.assertIs((await iterator.__anext__()).kind, VoiceEventKind.THINKING)
        current = await asyncio.wait_for(iterator.__anext__(), timeout=0.2)
        self.assertIs(current.kind, VoiceEventKind.ACTIVITY)
        self.assertEqual(current.activity, "coding")
        release.set()
        events = [event async for event in iterator]
        self.assertIn(VoiceEventKind.LISTENING, [event.kind for event in events])

    async def test_stream_wires_the_real_core_task_into_coding_liveness(self) -> None:
        """`_stream` must attach the actual `core_task`, not a stand-in.

        The unit-level `CodingTelemetryPresentationTests` prove the liveness
        formula is correct given some `owner_alive` callable. They do not
        prove `VoiceSession._stream` hands that callable the real
        `core_task` it created. This drives an actual Core turn on a real
        worker thread, blocked on a `threading.Event`, so `core_task.done()`
        reflects a genuine pending-then-completed lifecycle rather than a
        boolean the test supplies directly.
        """
        activity = VoiceActivityStatus()
        release = threading.Event()

        class BlockingGateway(FakeGateway):
            def receive_conversation_turn(self, turn, step_budget, retention_until):
                # Forces an ACTIVITY event onto the same queue `_stream`
                # already reads from, so the test can await proof that
                # `core_task` exists and `set_coding_owner_alive` has run
                # before it publishes telemetry, without guessing a delay.
                activity.set("coding")
                if not release.wait(timeout=1):
                    raise RuntimeError("test did not release the Core worker")
                return super().receive_conversation_turn(
                    turn, step_budget, retention_until
                )

        session = VoiceSession(
            BlockingGateway((outcome(GoalStatus.ACTIVE),)),
            FakeTranscriber((transcription("one", TranscriptionState.FINAL, "Hi"),)),
            FakeSynthesizer(), "friedl", 8, 3650,
            clock=lambda: NOW, identifier_factory=lambda: "turn-1",
            activity=activity,
        )
        iterator = session.exchange("conversation-1", incoming_audio())
        self.assertIs((await iterator.__anext__()).kind, VoiceEventKind.THINKING)
        current = await asyncio.wait_for(iterator.__anext__(), timeout=0.2)
        self.assertIs(current.kind, VoiceEventKind.ACTIVITY)

        # `_stream` has now created its own `core_task` and called
        # `set_coding_owner_alive(lambda: not core_task.done())` (proven
        # by the ACTIVITY event above, which only reaches the queue after
        # that wiring runs). The coding provider reports mid-flight while
        # that real task is still pending on the blocked worker thread.
        telemetry = CodingTelemetry(
            job_id="case-1", phase="execution",
            started_at=NOW - timedelta(minutes=1),
            phase_started_at=NOW - timedelta(seconds=30),
            last_activity_at=NOW - timedelta(seconds=200),
            in_flight=True,
        )
        activity.publish_coding(telemetry)
        running_snapshot = activity.coding_snapshot(NOW)
        self.assertTrue(running_snapshot["owner_alive"])
        self.assertFalse(running_snapshot["unresponsive"])
        self.assertFalse(running_snapshot["stalled"])

        # Let the real worker thread return, so the real `core_task`
        # genuinely completes, without publishing any terminal telemetry
        # first: the exact case where the coding job vanished.
        release.set()
        events = [event async for event in iterator]
        self.assertIn(VoiceEventKind.LISTENING, [event.kind for event in events])

        # The backend, not the browser, re-derives liveness from the same
        # nonterminal, still in_flight telemetry against the now-completed
        # real core_task.
        dead_snapshot = activity.coding_snapshot(NOW)
        self.assertFalse(dead_snapshot["owner_alive"])
        self.assertTrue(dead_snapshot["unresponsive"])
        self.assertTrue(dead_snapshot["stalled"])

    async def test_queued_turn_cannot_claim_first_turn_coding_owner(self) -> None:
        """A waiting exchange cannot bind telemetry before it owns Core's lock."""
        activity = VoiceActivityStatus()
        core_lock = asyncio.Lock()
        a_started = threading.Event()
        a_release = threading.Event()
        b_started = threading.Event()
        b_release = threading.Event()

        class TwoTurnGateway(FakeGateway):
            def __init__(self):
                super().__init__((outcome(GoalStatus.ACTIVE), outcome(GoalStatus.ACTIVE)))
                self.calls = 0

            def receive_conversation_turn(self, turn, step_budget, retention_until):
                self.calls += 1
                if self.calls == 1:
                    a_started.set()
                    if not a_release.wait(timeout=1):
                        raise RuntimeError("test did not release turn A")
                else:
                    b_started.set()
                    if not b_release.wait(timeout=1):
                        raise RuntimeError("test did not release turn B")
                return super().receive_conversation_turn(
                    turn, step_budget, retention_until
                )

        gateway = TwoTurnGateway()
        session = VoiceSession(
            gateway,
            FakeTranscriber((transcription("a", TranscriptionState.FINAL, "A"),)),
            FakeSynthesizer(), "friedl", 8, 3650,
            clock=lambda: NOW, identifier_factory=lambda: "turn",
            activity=activity, core_turn_lock=core_lock,
        )
        first = session.exchange("conversation-a", incoming_audio())
        self.assertIs((await first.__anext__()).kind, VoiceEventKind.THINKING)
        first_update = asyncio.create_task(first.__anext__())
        await asyncio.wait_for(asyncio.to_thread(a_started.wait, 1), timeout=1)

        # B starts and waits for the lock while A remains its owner. Its
        # transport reaches THINKING, but cannot install an owner callback.
        second = session.exchange("conversation-b", incoming_audio())
        self.assertIs((await second.__anext__()).kind, VoiceEventKind.THINKING)
        second_update = asyncio.create_task(second.__anext__())
        await asyncio.sleep(0)

        telemetry = CodingTelemetry(
            job_id="case-a", phase="execution", started_at=NOW,
            phase_started_at=NOW, last_activity_at=NOW,
            in_flight=True, transition="EXECUTION started",
        )
        activity.publish_coding(telemetry)
        self.assertIs((await first_update).kind, VoiceEventKind.DIAGNOSTIC)
        self.assertIs((await second_update).kind, VoiceEventKind.DIAGNOSTIC)
        self.assertTrue(activity.coding_snapshot(NOW)["owner_alive"])

        # A exits without terminal telemetry. B may now acquire the lock, but
        # the captured A owner is complete and cannot be replaced by B.
        a_release.set()
        await asyncio.wait_for(asyncio.to_thread(b_started.wait, 1), timeout=1)
        stale = activity.coding_snapshot(NOW)
        self.assertFalse(stale["owner_alive"])
        self.assertTrue(stale["unresponsive"])
        self.assertTrue(stale["stalled"])

        b_release.set()
        [event async for event in first]
        [event async for event in second]

    async def test_websocket_forwards_activity_transitions_in_the_active_exchange(self) -> None:
        """No second user turn or polling request is needed for terminal state."""
        class Session:
            async def exchange(self, *_args, **_kwargs):
                yield VoiceEvent(VoiceEventKind.ACTIVITY, activity="coding")
                yield VoiceEvent(VoiceEventKind.ACTIVITY, activity="reviewing")
                yield VoiceEvent(VoiceEventKind.LISTENING)

        sent: list[str] = []

        class Connection:
            async def send(self, payload):
                sent.append(payload)

        server = LiveVoiceServer.__new__(LiveVoiceServer)
        server._session = Session()
        server._await_audio_confirmation = False
        server._delivery_queues = {}
        server._typed_queues = {}

        async def audio():
            if False:
                yield AudioChunk("mic", 0, b"", "audio/pcm", 16000)

        server._audio = lambda _connection, _conversation: audio()
        await server._exchange_once(Connection(), "conversation-1")
        activities = [
            json.loads(frame)["value"]
            for frame in sent
            if json.loads(frame).get("type") == "activity"
        ]
        self.assertEqual(activities, ["coding", "reviewing"])

    async def test_websocket_forwards_backend_coding_liveness_without_inference(self) -> None:
        status = {
            "code": "coding.status", "job_id": "case-1", "phase": "execution",
            "in_flight": True, "owner_alive": False, "unresponsive": True,
            "stalled": True,
        }

        class Session:
            async def exchange(self, *_args, **_kwargs):
                yield VoiceEvent(VoiceEventKind.DIAGNOSTIC, diagnostic=status)
                yield VoiceEvent(VoiceEventKind.LISTENING)

        sent: list[str] = []

        class Connection:
            async def send(self, payload):
                sent.append(payload)

        server = LiveVoiceServer.__new__(LiveVoiceServer)
        server._session = Session()
        server._await_audio_confirmation = False
        server._delivery_queues = {}
        server._typed_queues = {}

        async def audio():
            if False:
                yield AudioChunk("mic", 0, b"", "audio/pcm", 16000)

        server._audio = lambda _connection, _conversation: audio()
        await server._exchange_once(Connection(), "conversation-1")
        forwarded = next(
            json.loads(frame) for frame in sent
            if json.loads(frame).get("code") == "coding.status"
        )
        self.assertEqual(forwarded, {"type": "diagnostic", **status})

    async def test_websocket_forwards_thinking_input_origin(self) -> None:
        class Session:
            async def exchange(self, *_args, **_kwargs):
                yield VoiceEvent(
                    VoiceEventKind.THINKING, input_origin="background_event",
                )
                yield VoiceEvent(VoiceEventKind.LISTENING)

        sent: list[str] = []

        class Connection:
            async def send(self, payload):
                sent.append(payload)

        server = LiveVoiceServer.__new__(LiveVoiceServer)
        server._session = Session()
        server._await_audio_confirmation = False
        server._delivery_queues = {}
        server._typed_queues = {}

        async def audio():
            if False:
                yield AudioChunk("mic", 0, b"", "audio/pcm", 16000)

        server._audio = lambda _connection, _conversation: audio()
        await server._exchange_once(Connection(), "conversation-1")
        thinking = next(
            json.loads(frame) for frame in sent
            if json.loads(frame).get("value") == "thinking"
        )
        self.assertEqual(
            thinking,
            {"type": "phase", "value": "thinking", "input_origin": "background_event"},
        )

    async def test_tts_transport_diagnostics_arrive_before_audio(self) -> None:
        diagnostics = VoiceDiagnosticBuffer()

        class DiagnosticSynthesizer:
            async def synthesize(self, response, correlation_id=None):
                for code in (
                    "tts.request_sent",
                    "tts.text_sent",
                    "tts.stream_connected",
                    "tts.first_audio_byte",
                ):
                    diagnostics.publish(
                        correlation_id,
                        {"code": code, "transport": "http", "elapsed_ms": 1},
                    )
                yield AudioChunk("tts", 0, b"spoken", "audio/mpeg")
                yield AudioChunk("tts", 1, b"", "audio/mpeg", final=True)

        session = VoiceSession(
            FakeGateway((outcome(GoalStatus.ACTIVE),)),
            FakeTranscriber((transcription("final", TranscriptionState.FINAL, "Hello"),)),
            DiagnosticSynthesizer(),
            "friedl", 8, 3650,
            clock=lambda: NOW,
            identifier_factory=lambda: "turn-1",
            diagnostics=diagnostics,
        )
        events = [
            event async for event in session.exchange("conversation-1", incoming_audio())
        ]
        first_audio = next(index for index, event in enumerate(events)
                           if event.kind is VoiceEventKind.AUDIO)
        codes = [
            event.diagnostic["code"]
            for event in events[:first_audio]
            if event.kind is VoiceEventKind.DIAGNOSTIC
        ]
        self.assertEqual(
            codes,
            [
                "tts.request_sent",
                "tts.text_sent",
                "tts.stream_connected",
                "tts.first_audio_byte",
            ],
        )

    async def test_transcript_enters_gateway_unchanged_and_only_core_response_is_spoken(self) -> None:
        transcriber = FakeTranscriber(
            (
                transcription("partial", TranscriptionState.PARTIAL, "Good"),
                transcription("final", TranscriptionState.FINAL, "Good morning ALX"),
            )
        )
        synthesizer = FakeSynthesizer()
        gateway = FakeGateway((outcome(GoalStatus.AWAITING_INPUT),))
        identifiers = iter(("turn-1",))
        session = VoiceSession(
            gateway,
            transcriber,
            synthesizer,
            "friedl",
            8,
            3650,
            clock=lambda: NOW,
            identifier_factory=lambda: next(identifiers),
        )

        events = [event async for event in session.exchange("conversation-1", incoming_audio())]

        turn, budget, retention_until = gateway.calls[0]
        self.assertEqual(turn.content, "Good morning ALX")
        self.assertEqual(turn.origin, ConversationOrigin.SPEECH_TRANSCRIPT)
        self.assertEqual(turn.person_id, "friedl")
        self.assertEqual(budget, 8)
        self.assertIsNotNone(retention_until)
        self.assertEqual(synthesizer.responses, ["authoritative response"])
        self.assertEqual(transcriber.received[0].payload, b"pcm")
        self.assertNotEqual(gateway.thread_ids, [threading.get_ident()])
        self.assertEqual(
            [event.kind for event in events],
            [
                VoiceEventKind.HEARING,
                VoiceEventKind.THINKING,
                # Her wording reaches the console before it is spoken, so a
                # silent runtime still shows what she said.
                VoiceEventKind.TEXT,
                VoiceEventKind.SPEAKING,
                VoiceEventKind.AUDIO,
                VoiceEventKind.AUDIO,
                VoiceEventKind.LISTENING,
            ],
        )

    async def test_follow_up_has_no_phrase_or_goal_routing_in_voice_interface(self) -> None:
        transcriber = FakeTranscriber(
            (
                transcription("one", TranscriptionState.FINAL, "First thought"),
                transcription("two", TranscriptionState.FINAL, "Actually, change it"),
            )
        )
        gateway = FakeGateway(
            (
                outcome(GoalStatus.AWAITING_INPUT, "first response"),
                outcome(GoalStatus.AWAITING_INPUT, "second response"),
            )
        )
        identifiers = iter(("turn-1", "turn-2"))
        session = VoiceSession(
            gateway,
            transcriber,
            FakeSynthesizer(),
            "friedl",
            8,
            3650,
            clock=lambda: NOW,
            identifier_factory=lambda: next(identifiers),
        )

        _ = [event async for event in session.exchange("conversation-1", incoming_audio())]

        self.assertEqual(
            [call[0].content for call in gateway.calls],
            ["First thought", "Actually, change it"],
        )
        self.assertTrue(all(call[2] is not None for call in gateway.calls))

    async def test_rejected_core_decision_reports_error_and_resumes_listening(self) -> None:
        transcriber = FakeTranscriber(
            (
                transcription("one", TranscriptionState.FINAL, "First thought"),
                transcription("two", TranscriptionState.FINAL, "Try again"),
            )
        )
        synthesizer = FakeSynthesizer()
        gateway = FakeGateway(
            (
                outcome(
                    GoalStatus.ACTIVE,
                    response=None,
                    reason="decision_invalid",
                    core_state=CoreState.ERROR,
                ),
                outcome(GoalStatus.AWAITING_INPUT, "recovered response"),
            )
        )
        identifiers = iter(("turn-1", "turn-2"))
        session = VoiceSession(
            gateway,
            transcriber,
            synthesizer,
            "friedl",
            8,
            3650,
            clock=lambda: NOW,
            identifier_factory=lambda: next(identifiers),
        )

        events = [event async for event in session.exchange("conversation-1", incoming_audio())]

        self.assertEqual(
            [event.kind for event in events],
            [
                VoiceEventKind.THINKING,
                VoiceEventKind.ERROR,
                VoiceEventKind.LISTENING,
                VoiceEventKind.THINKING,
                # Her wording reaches the console before it is spoken, so a
                # silent runtime still shows what she said.
                VoiceEventKind.TEXT,
                VoiceEventKind.SPEAKING,
                VoiceEventKind.AUDIO,
                VoiceEventKind.AUDIO,
                VoiceEventKind.LISTENING,
            ],
        )
        self.assertEqual(events[1].reason, "decision_invalid")
        self.assertEqual(synthesizer.responses, ["recovered response"])

    async def test_core_selected_silence_skips_tts_without_becoming_an_error(self) -> None:
        synthesizer = FakeSynthesizer()
        gateway = FakeGateway((outcome(
            GoalStatus.ACTIVE,
            response=None,
            reason="core_selected_silence",
            core_state=CoreState.FINISHED_SILENTLY,
        ),))
        session = VoiceSession(
            gateway,
            FakeTranscriber((transcription(
                "one", TranscriptionState.FINAL, "No answer is needed"
            ),)),
            synthesizer,
            "friedl", 8, 3650,
            clock=lambda: NOW,
            identifier_factory=lambda: "turn-1",
        )
        events = [
            event async for event in session.exchange(
                "conversation-1", incoming_audio()
            )
        ]
        self.assertEqual(
            [event.kind for event in events],
            [VoiceEventKind.THINKING, VoiceEventKind.LISTENING],
        )
        self.assertEqual(synthesizer.responses, [])

    async def test_missing_response_is_still_an_error_not_silence(self) -> None:
        session = VoiceSession(
            FakeGateway((outcome(
                GoalStatus.ACTIVE,
                response=None,
                reason="active_goal_required",
                core_state=CoreState.CHECKPOINTED,
            ),)),
            FakeTranscriber((transcription(
                "one", TranscriptionState.FINAL, "Do the required action"
            ),)),
            FakeSynthesizer(),
            "friedl", 8, 3650,
            clock=lambda: NOW,
            identifier_factory=lambda: "turn-1",
        )
        events = [
            event async for event in session.exchange(
                "conversation-1", incoming_audio()
            )
        ]
        self.assertEqual(
            [event.kind for event in events],
            [VoiceEventKind.THINKING, VoiceEventKind.ERROR, VoiceEventKind.LISTENING],
        )
        self.assertEqual(events[1].reason, "active_goal_required")


class BootstrapVoiceTests(unittest.TestCase):
    def test_legacy_goal_owned_turns_migrate_to_independent_conversation_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            goal_path = Path(directory) / "goals.sqlite3"
            conversation_path = Path(directory) / "conversations.sqlite3"
            state = GoalState(
                "goal-1", Objective("turn:turn-1", "objective"),
                (SuccessCriterion("criterion-1", "success"),),
            )
            turn = ConversationTurn(
                "conversation-1", "turn-1", ConversationOrigin.TYPED,
                "preserve me", NOW, "friedl",
            )
            connection = sqlite3.connect(goal_path)
            connection.execute(
                "CREATE TABLE goals (goal_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, retention_until TEXT NOT NULL, state_json TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE conversation_turns (goal_id TEXT NOT NULL REFERENCES goals(goal_id) ON DELETE CASCADE, ordinal INTEGER NOT NULL, turn_id TEXT NOT NULL, turn_json TEXT NOT NULL, PRIMARY KEY(goal_id, ordinal), UNIQUE(goal_id, turn_id))"
            )
            connection.execute(
                "CREATE TABLE pending_memory_batches (goal_id TEXT NOT NULL REFERENCES goals(goal_id) ON DELETE CASCADE, goal_revision INTEGER NOT NULL, ordinal INTEGER NOT NULL, proposal_json TEXT NOT NULL, retention_until TEXT NOT NULL, PRIMARY KEY(goal_id, goal_revision, ordinal))"
            )
            connection.execute(
                "INSERT INTO goals VALUES (?, ?, ?, ?)",
                ("goal-1", 1, NOW.isoformat(), json.dumps(_goal_to_data(state))),
            )
            connection.execute(
                "INSERT INTO conversation_turns VALUES (?, ?, ?, ?)",
                ("goal-1", 0, "turn-1", json.dumps([
                    turn.conversation_id, turn.turn_id, turn.origin.value,
                    turn.content, turn.occurred_at.isoformat(), turn.person_id,
                ])),
            )
            connection.execute("PRAGMA user_version = 3")
            connection.commit()
            connection.close()
            goals = SQLiteGoalStore(goal_path)
            conversations = SQLiteConversationStore(conversation_path)
            try:
                migrate_legacy_conversations(goals, conversations)
                self.assertEqual(conversations.load("conversation-1").turns, (turn,))
                self.assertEqual(goals.load("goal-1").conversation_id, "conversation-1")
                migrate_legacy_conversations(goals, conversations)
                self.assertEqual(conversations.load("conversation-1").turns, (turn,))
            finally:
                conversations.close()
                goals.close()

    def test_no_deterministic_goal_selection_remains_in_the_composition_root(self) -> None:
        """Law 1: choosing which goal an input belongs to is AL/X's judgement.

        The runtime used to attach the newest unfinished goal before the Core
        reasoned, which bound a mail deletion to an unrelated research goal.
        Nothing in the composition root or the gateway may select a goal by
        recency, domain, capability or wording.
        """
        import alx.bootstrap.live_voice as bootstrap
        from alx.conversation import gateway

        self.assertFalse(hasattr(bootstrap, "locate_active_goal"))
        for module in (bootstrap, gateway):
            source = Path(module.__file__).read_text("utf-8")
            for superseded in (
                "locate_active_goal", "ActiveGoalLocator", "candidates[-1]",
            ):
                self.assertNotIn(
                    superseded, source,
                    f"{Path(module.__file__).name} still selects a goal deterministically",
                )

    def test_process_environment_overrides_file_without_executing_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "ALX_INTERFACE_HOST=file-host\nIGNORED COMMAND\nVALUE='quoted'\n",
                encoding="utf-8",
            )
            values = load_environment(path, {"ALX_INTERFACE_HOST": "process-host"})

        self.assertEqual(values["ALX_INTERFACE_HOST"], "process-host")
        self.assertEqual(values["VALUE"], "quoted")
        self.assertNotIn("IGNORED COMMAND", values)




class BudgetRecoveryReservationTests(unittest.IsolatedAsyncioTestCase):
    """The recovery allowance belongs to the person, not to background work.

    On 2026-09-08 a DHL import spent the conversation's ceiling, checkpointed
    in a millisecond, and background turns cycled through the one recovery
    allowance before Friedl could type. 213 instant checkpoints later the
    conversation could not reason at all, and his next message was discarded
    in seven milliseconds without reaching the model.
    """

    def _recorder(self):
        from alx.observability import SQLiteUsageRecorder

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return SQLiteUsageRecorder(Path(directory.name) / "usage.sqlite3")

    def _exhausted(self, recorder, budget, conversation="conversation-1"):
        """Spend the whole ceiling, as a real runaway task would."""
        recorder.set_budget(conversation, budget)
        for index in range(budget.stop_above):
            recorder.record(
                conversation,
                {
                    "code": "reasoning.completed",
                    "provider": "claude_subscription",
                    "model": "claude-sonnet-5",
                    "input_tokens": 10,
                    "output_tokens": 10,
                    "duration_ms": 1,
                },
            )
        return conversation

    def _production_callbacks(self, recorder):
        """Compile the actual bootstrap callbacks, without starting live IO.

        No budget/origin logic is reproduced here. Both nodes are taken from
        run(), so mutations to production affect these behavioural tests.
        """
        import ast
        from alx.observability import BudgetExceeded

        source = Path(__file__).resolve().parents[1] / "src/alx/bootstrap/live_voice.py"
        tree = ast.parse(source.read_text())
        run = next(node for node in tree.body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name == "run")
        callback = next(node for node in run.body
                        if isinstance(node, ast.FunctionDef) and node.name == "budget_check")
        sink = next(node.value for node in ast.walk(run)
                    if isinstance(node, ast.keyword) and node.arg == "turn_origin_sink")
        namespace = {"usage": recorder, "BudgetExceeded": BudgetExceeded,
                     "person_turn_in_progress": [False], "current_conversation_id": [""]}
        exec(compile(ast.Module(body=[callback], type_ignores=[]), str(source), "exec"), namespace)
        origin_sink = eval(compile(ast.Expression(body=sink), str(source), "eval"), namespace)

        return namespace["budget_check"], origin_sink

    def _budget_check(self, recorder, person_flag):
        callback, origin_sink = self._production_callbacks(recorder)

        def check(conversation_id):
            origin_sink(person_flag[0])
            return callback(conversation_id)

        return check

    def test_background_turns_cannot_consume_the_recovery_allowance(self) -> None:
        from alx.observability import BudgetExceeded
        from alx.observability.usage import bill_budget_for

        recorder = self._recorder()
        budget = bill_budget_for("claude_subscription")
        conversation = self._exhausted(recorder, budget)
        person = [False]
        check = self._budget_check(recorder, person)

        # Many background turns hit the exhausted ceiling.
        for _ in range(50):
            with self.assertRaises(BudgetExceeded):
                check(conversation)

        # The allowance is untouched, so the person still has it.
        self.assertNotIn(conversation, recorder._recovery)

    def test_the_next_person_turn_still_receives_recovery(self) -> None:
        from alx.observability import BudgetExceeded
        from alx.observability.usage import bill_budget_for

        recorder = self._recorder()
        budget = bill_budget_for("claude_subscription")
        conversation = self._exhausted(recorder, budget)
        person = [False]
        check = self._budget_check(recorder, person)
        for _ in range(50):
            with self.assertRaises(BudgetExceeded):
                check(conversation)

        # Friedl speaks. The first check still raises - the window is spent -
        # but it declares recovery, so the turns after it may reason.
        person[0] = True
        with self.assertRaises(BudgetExceeded):
            check(conversation)
        self.assertIn(conversation, recorder._recovery)
        check(conversation)  # must not raise: the allowance is available

    def test_recovery_remains_bounded(self) -> None:
        """Requirement 4: recovery buys an allowance, never an open budget."""
        from alx.observability import BudgetExceeded
        from alx.observability.usage import bill_budget_for

        recorder = self._recorder()
        budget = bill_budget_for("claude_subscription")
        conversation = self._exhausted(recorder, budget)
        person = [True]
        check = self._budget_check(recorder, person)
        with self.assertRaises(BudgetExceeded):
            check(conversation)

        for _ in range(budget.recovery_allowance):
            check(conversation)
            recorder.record(
                conversation,
                {
                    "code": "reasoning.completed",
                    "provider": "claude_subscription",
                    "model": "claude-sonnet-5",
                    "input_tokens": 10,
                    "output_tokens": 10,
                    "duration_ms": 1,
                },
            )
        with self.assertRaises(BudgetExceeded):
            check(conversation)

    async def test_person_checkpoint_reserves_recovery_and_resets_origin(self):
        from alx.observability import BudgetExceeded
        from alx.observability.usage import bill_budget_for

        recorder = self._recorder()
        conversation = self._exhausted(recorder, bill_budget_for("claude_subscription"))
        check, sink = self._production_callbacks(recorder)
        test = self

        class Gateway(FakeGateway):
            def receive_conversation_turn(self, turn, *args):
                self.calls.append(turn)
                with test.assertRaises(BudgetExceeded):
                    check(conversation)
                return outcome(GoalStatus.ACTIVE, None, reason="budget_exceeded",
                               core_state=CoreState.CHECKPOINTED)

        gateway = Gateway(())
        typed = asyncio.Queue()
        await typed.put("continue")
        session = VoiceSession(
            gateway, OpenTranscriber(),
            FakeSynthesizer(), "friedl", 8, 3650, clock=lambda: NOW,
            turn_origin_sink=sink,
        )
        iterator = session.exchange(conversation, incoming_audio(), typed=typed)
        try:
            for _ in range(20):
                try:
                    await asyncio.wait_for(iterator.__anext__(), 0.1)
                except (asyncio.TimeoutError, StopAsyncIteration):
                    break
            self.assertEqual(len(gateway.calls), 1)
            self.assertIn(conversation, recorder._recovery)
            # After the person exits, a caller outside VoiceSession (including
            # autonomous work) has no person origin and cannot spend recovery.
            with self.assertRaises(BudgetExceeded):
                check(conversation)
            sink(True)
            check(conversation)
            sink(False)
        finally:
            await iterator.aclose()

    async def test_gateway_error_clears_origin_before_releasing_lock(self):
        from alx.observability import BudgetExceeded
        from alx.observability.usage import bill_budget_for

        recorder = self._recorder()
        conversation = self._exhausted(recorder, bill_budget_for("claude_subscription"))
        check, sink = self._production_callbacks(recorder)

        class Gateway:
            def receive_conversation_turn(self, *args):
                raise RuntimeError("failed before budget check")

        typed = asyncio.Queue()
        await typed.put("continue")
        lock = asyncio.Lock()
        observed = []
        def observing_sink(person):
            observed.append((person, lock.locked()))
            sink(person)
        session = VoiceSession(
            Gateway(), OpenTranscriber(),
            FakeSynthesizer(), "friedl", 8, 3650, clock=lambda: NOW,
            turn_origin_sink=observing_sink, core_turn_lock=lock,
        )
        events = [event async for event in session.exchange(
            conversation, incoming_audio(), typed=typed)]
        self.assertTrue(any(event.kind is VoiceEventKind.ERROR for event in events))
        self.assertEqual(observed, [(True, True), (False, True)])
        with self.assertRaises(BudgetExceeded):
            check(conversation)
        self.assertNotIn(conversation, recorder._recovery)

    def test_background_cannot_spend_an_already_granted_allowance(self):
        from alx.observability import BudgetExceeded
        from alx.observability.usage import bill_budget_for

        recorder = self._recorder()
        conversation = self._exhausted(recorder, bill_budget_for("claude_subscription"))
        check, sink = self._production_callbacks(recorder)
        sink(True)
        with self.assertRaises(BudgetExceeded):
            check(conversation)
        sink(False)
        for _ in range(3):
            with self.assertRaises(BudgetExceeded):
                check(conversation)
        sink(True)
        check(conversation)

    def test_a_new_conversation_gets_a_fresh_budget(self) -> None:
        from alx.observability.usage import bill_budget_for

        recorder = self._recorder()
        budget = bill_budget_for("claude_subscription")
        self._exhausted(recorder, budget, "conversation-1")
        # Reloading the page starts a new conversation id.
        recorder.check("conversation-2")  # must not raise


if __name__ == "__main__":
    unittest.main()
