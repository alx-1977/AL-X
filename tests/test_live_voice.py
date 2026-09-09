from __future__ import annotations

import asyncio
import tempfile
import unittest
import json
import sqlite3
import threading
from datetime import UTC, datetime
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
from alx.interfaces import VoiceDiagnosticBuffer, VoiceEventKind, VoiceSession  # noqa: E402
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


class FakeGateway:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []
        self.thread_ids = []
        self.background_calls = []
        # The sequence turns actually reached the Core in, which is what a
        # starvation test has to assert on: counts alone cannot tell whether
        # the person waited behind the backlog or ran before it.
        self.turn_order = []

    def receive_conversation_turn(self, turn, step_budget, retention_until):
        self.thread_ids.append(threading.get_ident())
        self.calls.append((turn, step_budget, retention_until))
        self.turn_order.append("person")
        return next(self.outcomes)

    def receive_background_event(
        self, conversation_id, event, step_budget, retention_until
    ):
        self.thread_ids.append(threading.get_ident())
        self.background_calls.append(
            (conversation_id, event, step_budget, retention_until)
        )
        self.turn_order.append("background")
        return next(self.outcomes)


class FakeEventSource:
    def __init__(self, event):
        self.event = event
        self.delivered = []

    async def events(self):
        yield self.event
        await __import__("asyncio").Future()

    def record_delivery(self, event_id):
        self.delivered.append(event_id)


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


class VoiceSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_event_enters_same_gateway_and_only_core_response_is_spoken(self) -> None:
        event = BackgroundEvent(
            "mail:777:2",
            "mail.message_arrived",
            NOW,
            {"mailbox_id": "INBOX", "uid": "2"},
        )
        source = FakeEventSource(event)
        gateway = FakeGateway((outcome(GoalStatus.AWAITING_INPUT, "Mail summary"),))
        synthesizer = FakeSynthesizer()
        session = VoiceSession(
            gateway,
            FakeTranscriber(()),
            synthesizer,
            "friedl",
            8,
            3650,
            clock=lambda: NOW,
            diagnostics=None,
            event_source=source,
        )
        iterator = session.exchange("conversation-1", incoming_audio())
        observed = []
        async for item in iterator:
            observed.append(item)
            if item.kind is VoiceEventKind.LISTENING:
                break
        import asyncio
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(iterator.__anext__(), timeout=0.01)
        self.assertEqual(gateway.background_calls[0][1], event)
        self.assertEqual(synthesizer.responses, ["Mail summary"])
        self.assertEqual(source.delivered, [event.event_id])
        self.assertEqual(
            [item.kind for item in observed],
            [
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


if __name__ == "__main__":
    unittest.main()


class CoreTurnStarvationTests(unittest.IsolatedAsyncioTestCase):
    """A person waiting must not sit behind a backlog of background work.

    On 2026-09-08 five consecutive background turns ran while two typed
    messages were never processed at all. Mail observations re-emit every poll
    cycle until a turn records their delivery, and a background turn takes
    longer than the poll interval, so the one first-in-first-out queue gained
    events faster than it drained. Typed input added behind that backlog was
    never reached.
    """

    @staticmethod
    def _events(count):
        return tuple(
            BackgroundEvent(
                f"mail:777:{index}",
                "mail.message_arrived",
                NOW,
                {"mailbox_id": "INBOX", "uid": str(index)},
            )
            for index in range(count)
        )

    class _Source:
        """Emits a backlog at once, exactly as a re-emitting poll cycle does."""

        def __init__(self, events):
            self._events = events
            self.delivered = []

        async def events(self):
            for event in self._events:
                yield event
            await asyncio.Future()

        def record_delivery(self, event_id):
            self.delivered.append(event_id)
            return True

    async def _drain(self, session, conversation_id="conversation-1", limit=40):
        iterator = session.exchange(
            conversation_id, incoming_audio(), typed=self.typed
        )
        seen = 0
        async for _ in iterator:
            seen += 1
            if seen >= limit:
                break
            try:
                await asyncio.wait_for(asyncio.sleep(0), timeout=0.01)
            except asyncio.TimeoutError:  # pragma: no cover - defensive
                break

    def setUp(self) -> None:
        self.typed = asyncio.Queue()

    async def test_a_person_runs_before_the_next_background_turn(self) -> None:
        """Requirement 1 and 2: person input takes the Core first."""
        events = self._events(4)
        source = self._Source(events)
        gateway = FakeGateway(
            tuple(outcome(GoalStatus.ACTIVE, "answer") for _ in range(6))
        )
        session = VoiceSession(
            gateway,
            FakeTranscriber(()),
            FakeSynthesizer(),
            "friedl",
            8,
            3650,
            clock=lambda: NOW,
            event_source=source,
        )
        await self.typed.put("Hey ALX!")
        iterator = session.exchange(
            "conversation-1", incoming_audio(), typed=self.typed
        )
        # Run until the person turn has been served.
        for _ in range(30):
            try:
                await asyncio.wait_for(iterator.__anext__(), timeout=1.0)
            except (StopAsyncIteration, asyncio.TimeoutError):
                break
            if gateway.calls:
                break
        self.assertEqual(
            len(gateway.calls), 1, "the typed turn never reached the Core"
        )
        self.assertEqual(gateway.calls[0][0].content, "Hey ALX!")
        # And it did not wait for the whole backlog first.
        self.assertLess(
            len(gateway.background_calls),
            len(events),
            "the person waited behind the entire backlog",
        )

    async def test_repeated_silent_background_turns_cannot_starve(self) -> None:
        """Requirement 5: silence must not build an endless queue ahead."""
        source = self._Source(self._events(12))
        gateway = FakeGateway(
            tuple(
                outcome(
                    GoalStatus.ACTIVE,
                    None,
                    reason="core_selected_silence",
                    core_state=CoreState.FINISHED_SILENTLY,
                )
                for _ in range(12)
            )
            + (outcome(GoalStatus.ACTIVE, "answer"),)
        )
        session = VoiceSession(
            gateway,
            FakeTranscriber(()),
            FakeSynthesizer(),
            "friedl",
            8,
            3650,
            clock=lambda: NOW,
            event_source=source,
        )
        await self.typed.put("Hello")
        iterator = session.exchange(
            "conversation-1", incoming_audio(), typed=self.typed
        )
        for _ in range(40):
            try:
                await asyncio.wait_for(iterator.__anext__(), timeout=1.0)
            except (StopAsyncIteration, asyncio.TimeoutError):
                break
            if gateway.calls:
                break
        self.assertEqual(
            len(gateway.calls), 1, "silent background turns starved the person"
        )

    async def test_background_work_resumes_after_the_person_turn(self) -> None:
        """Requirement 4: nothing is dropped, only reordered."""
        events = self._events(3)
        source = self._Source(events)
        gateway = FakeGateway(
            tuple(outcome(GoalStatus.ACTIVE, "answer") for _ in range(8))
        )
        session = VoiceSession(
            gateway,
            FakeTranscriber(()),
            FakeSynthesizer(),
            "friedl",
            8,
            3650,
            clock=lambda: NOW,
            event_source=source,
        )
        await self.typed.put("Hey ALX!")
        iterator = session.exchange(
            "conversation-1", incoming_audio(), typed=self.typed
        )
        for _ in range(60):
            try:
                await asyncio.wait_for(iterator.__anext__(), timeout=1.0)
            except (StopAsyncIteration, asyncio.TimeoutError):
                break
            if gateway.calls and len(gateway.background_calls) >= len(events):
                break
        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual(
            len(gateway.background_calls),
            len(events),
            "deferred background work was dropped rather than resumed",
        )
        self.assertEqual(
            [call[1].event_id for call in gateway.background_calls],
            [event.event_id for event in events],
            "deferred background work lost its arrival order",
        )

    async def test_d024_background_cognition_still_runs(self) -> None:
        """Requirement 6: D-024 is not disabled, only ordered behind a person."""
        event = self._events(1)[0]
        source = self._Source((event,))
        gateway = FakeGateway((outcome(GoalStatus.AWAITING_INPUT, "Mail summary"),))
        session = VoiceSession(
            gateway,
            FakeTranscriber(()),
            FakeSynthesizer(),
            "friedl",
            8,
            3650,
            clock=lambda: NOW,
            event_source=source,
        )
        iterator = session.exchange("conversation-1", incoming_audio())
        async for item in iterator:
            if item.kind is VoiceEventKind.LISTENING:
                break
        # Delivery is recorded after the LISTENING event, so let the loop
        # reach its next wait before asserting.
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(iterator.__anext__(), timeout=0.05)
        self.assertEqual(gateway.background_calls[0][1], event)
        self.assertEqual(source.delivered, [event.event_id])

    class _OpenTranscriber:
        """Stays open, as a live session does, so typed input is reachable.

        An exhausted transcriber ends the exchange before anything typed is
        read. That is pre-existing behaviour and unrelated to ordering, but it
        makes an empty transcriber the wrong fixture for a typed-input test.
        """

        async def transcribe(self, audio):
            await asyncio.Future()
            yield  # pragma: no cover - never reached

    async def test_person_only_behaviour_is_unchanged(self) -> None:
        """Requirement: no event source, no deferral, identical path."""
        gateway = FakeGateway((outcome(GoalStatus.ACTIVE, "answer"),))
        session = VoiceSession(
            gateway,
            self._OpenTranscriber(),
            FakeSynthesizer(),
            "friedl",
            8,
            3650,
            clock=lambda: NOW,
        )
        await self.typed.put("Hey ALX!")
        iterator = session.exchange(
            "conversation-1", incoming_audio(), typed=self.typed
        )
        for _ in range(20):
            try:
                await asyncio.wait_for(iterator.__anext__(), timeout=1.0)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                continue
            if gateway.calls:
                break
        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual(gateway.calls[0][0].content, "Hey ALX!")


class CoreTurnStarvationRegressionTests(unittest.IsolatedAsyncioTestCase):
    """The live failure ordering, reproduced deterministically.

    An earlier version of these tests put the typed line in before the
    background events, so the person item was already first in the queue and
    won with or without the ordering fix. They passed against the unfixed code
    and proved nothing.

    The live sequence is the opposite: mail observations are already queued,
    Friedl types while they are draining, and his line lands *behind* the
    backlog. That is what is reproduced here - the backlog is enqueued first,
    and the typed line is enqueued after it but before the consumer reads.
    """

    class _BacklogSource:
        """Fills the queue with background work, then lets a person type.

        The typed line is queued from inside this generator, after the
        backlog has been emitted, so the consumer's first read faces exactly
        the live ordering: N background items ahead of one person item.
        """

        def __init__(self, count, typed, line="Hey ALX!"):
            self.queued = tuple(
                BackgroundEvent(
                    f"mail:777:{index}",
                    "mail.message_arrived",
                    NOW,
                    {"mailbox_id": "INBOX", "uid": str(index)},
                )
                for index in range(count)
            )
            self._typed = typed
            self._line = line
            self.delivered = []

        async def events(self):
            for event in self.queued:
                yield event
            # Everything above is now sitting in the one queue. The person
            # types at this moment, so the line goes in behind the backlog.
            await self._typed.put(self._line)
            await asyncio.Future()

        def record_delivery(self, event_id):
            self.delivered.append(event_id)
            return True

    class _GrowingBacklogSource(_BacklogSource):
        """Keeps producing background work while the person waits.

        This is the condition that made the live failure permanent: the queue
        gained events faster than it drained, so a person behind the backlog
        was never reached at all.
        """

        async def events(self):
            for event in self.queued[:3]:
                yield event
            await self._typed.put(self._line)
            # More arrive while the person is waiting, exactly as a re-emitting
            # poll cycle does.
            for event in self.queued[3:]:
                yield event
            await asyncio.Future()

    class _OpenTranscriber:
        async def transcribe(self, audio):
            await asyncio.Future()
            yield  # pragma: no cover - never reached

    async def _run(self, source, gateway, typed, reads=90):
        session = VoiceSession(
            gateway,
            self._OpenTranscriber(),
            FakeSynthesizer(),
            "friedl",
            8,
            3650,
            clock=lambda: NOW,
            event_source=source,
        )
        iterator = session.exchange(
            "conversation-1", incoming_audio(), typed=typed
        )
        for _ in range(reads):
            try:
                await asyncio.wait_for(iterator.__anext__(), timeout=0.5)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                break
        return gateway

    @staticmethod
    def _order(gateway):
        """Which turn kind reached the Core first, by call sequence."""
        return gateway.turn_order

    async def test_a_person_queued_behind_a_backlog_is_served_first(self) -> None:
        typed = asyncio.Queue()
        source = self._BacklogSource(6, typed)
        gateway = FakeGateway(
            tuple(outcome(GoalStatus.ACTIVE, "answer") for _ in range(12))
        )
        await self._run(source, gateway, typed)

        self.assertEqual(
            len(gateway.calls), 1, "the typed turn never reached the Core"
        )
        self.assertEqual(gateway.calls[0][0].content, "Hey ALX!")
        # The first background turn was already in flight when Friedl typed,
        # which no ordering rule can undo. What matters is that his line was
        # taken next rather than after the whole backlog: with six events
        # queued, at most one may precede him.
        background_before_person = gateway.turn_order.index("person")
        self.assertLessEqual(
            background_before_person,
            1,
            f"the person waited behind the backlog: {gateway.turn_order}",
        )

    async def test_the_backlog_is_deferred_not_dropped(self) -> None:
        """A safety property, not a starvation proof.

        This passes with or without the ordering fix, deliberately: it guards
        the thing the fix must not break - background work being lost or
        reordered - rather than the starvation itself. The two tests either
        side of it are the ones that fail without the fix.
        """
        typed = asyncio.Queue()
        source = self._BacklogSource(6, typed)
        gateway = FakeGateway(
            tuple(outcome(GoalStatus.ACTIVE, "answer") for _ in range(12))
        )
        await self._run(source, gateway, typed)

        self.assertEqual(
            len(gateway.background_calls),
            len(source.queued),
            "deferred background work was dropped",
        )
        self.assertEqual(
            [call[1].event_id for call in gateway.background_calls],
            [event.event_id for event in source.queued],
            "deferred background work lost its arrival order",
        )
        self.assertIn("person", gateway.turn_order)
        self.assertEqual(
            gateway.turn_order.count("background"),
            len(source.queued),
            f"background did not resume after the person: {gateway.turn_order}",
        )

    async def test_a_growing_backlog_cannot_starve_the_person(self) -> None:
        typed = asyncio.Queue()
        source = self._GrowingBacklogSource(8, typed)
        gateway = FakeGateway(
            tuple(outcome(GoalStatus.ACTIVE, "answer") for _ in range(16))
        )
        await self._run(source, gateway, typed)

        self.assertEqual(
            len(gateway.calls), 1, "a growing backlog starved the person turn"
        )
        background_before_person = gateway.turn_order.index("person")
        self.assertLessEqual(
            background_before_person,
            1,
            f"a growing backlog outran the person: {gateway.turn_order}",
        )


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

            def receive_background_event(self, *args):
                self.background_calls.append(args)
                return outcome(GoalStatus.ACTIVE, "unexpected")

        gateway = Gateway(())
        typed = asyncio.Queue()
        await typed.put("continue")
        session = VoiceSession(
            gateway, BackgroundCheckpointStormTests._OpenTranscriber(),
            FakeSynthesizer(), "friedl", 8, 3650, clock=lambda: NOW,
            event_source=BackgroundCheckpointStormTests._Source(3),
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
            self.assertEqual(gateway.background_calls, [])
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
            Gateway(), BackgroundCheckpointStormTests._OpenTranscriber(),
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


class BackgroundCheckpointStormTests(unittest.IsolatedAsyncioTestCase):
    """A background turn stopped on the budget must not immediately repeat."""

    class _Source:
        def __init__(self, count):
            self.queued = tuple(
                BackgroundEvent(
                    f"mail:777:{index}",
                    "mail.message_arrived",
                    NOW,
                    {"mailbox_id": "INBOX", "uid": str(index)},
                )
                for index in range(count)
            )
            self.delivered = []

        async def events(self):
            for event in self.queued:
                yield event
            await asyncio.Future()

        def record_delivery(self, event_id):
            self.delivered.append(event_id)
            return True

    class _OpenTranscriber:
        async def transcribe(self, audio):
            await asyncio.Future()
            yield  # pragma: no cover - never reached

    async def test_repeated_observations_are_coalesced_while_suppressed(self) -> None:
        """Re-polls cannot grow the deferred queue; distinct mail stays ordered."""
        first_finished = threading.Event()
        typed = asyncio.Queue()
        repeated = BackgroundEvent(
            "mail:777:repeat",
            "mail.message_arrived",
            NOW,
            {"mailbox_id": "INBOX", "uid": "repeat"},
        )
        distinct = tuple(
            BackgroundEvent(
                f"mail:777:{index}",
                "mail.message_arrived",
                NOW,
                {"mailbox_id": "INBOX", "uid": str(index)},
            )
            for index in range(3)
        )

        class RepeatingSource:
            def __init__(self):
                self.delivered = []

            async def events(source_self):
                yield repeated
                while not first_finished.is_set():
                    await asyncio.sleep(0)
                for _ in range(2_000):
                    yield repeated
                for event in distinct:
                    yield event
                await typed.put("person")
                await asyncio.Future()

            def record_delivery(source_self, event_id):
                source_self.delivered.append(event_id)
                return True

        class Gateway(FakeGateway):
            def receive_background_event(self, *args):
                result = super().receive_background_event(*args)
                first_finished.set()
                return result

        source = RepeatingSource()
        gateway = Gateway(
            (
                outcome(
                    GoalStatus.ACTIVE,
                    None,
                    reason="budget_exceeded",
                    core_state=CoreState.CHECKPOINTED,
                ),
                outcome(GoalStatus.ACTIVE, "person answer"),
            )
            + tuple(
                outcome(GoalStatus.ACTIVE, "background answer")
                for _ in range(4)
            )
        )
        session = VoiceSession(
            gateway,
            self._OpenTranscriber(),
            FakeSynthesizer(),
            "friedl",
            8,
            3650,
            clock=lambda: NOW,
            event_source=source,
        )
        iterator = session.exchange(
            "conversation-1", incoming_audio(), typed=typed
        )
        try:
            for _ in range(40):
                try:
                    await asyncio.wait_for(iterator.__anext__(), timeout=0.2)
                except asyncio.TimeoutError:
                    break
            background_ids = [
                call[1].event_id for call in gateway.background_calls
            ]
            self.assertEqual(gateway.turn_order[:2], ["background", "person"])
            self.assertEqual(
                background_ids,
                [repeated.event_id, repeated.event_id]
                + [event.event_id for event in distinct],
            )
            self.assertEqual(
                source.delivered,
                [repeated.event_id] + [event.event_id for event in distinct],
            )
        finally:
            await iterator.aclose()

    async def test_background_work_stops_after_one_budget_checkpoint(self) -> None:
        source = self._Source(40)
        gateway = FakeGateway(
            tuple(
                outcome(
                    GoalStatus.ACTIVE,
                    None,
                    reason="budget_exceeded",
                    core_state=CoreState.CHECKPOINTED,
                )
                for _ in range(60)
            )
        )
        session = VoiceSession(
            gateway,
            self._OpenTranscriber(),
            FakeSynthesizer(),
            "friedl",
            8,
            3650,
            clock=lambda: NOW,
            event_source=source,
        )
        iterator = session.exchange("conversation-1", incoming_audio())
        for _ in range(60):
            try:
                await asyncio.wait_for(iterator.__anext__(), timeout=0.3)
            except (StopAsyncIteration, asyncio.TimeoutError):
                break

        self.assertLessEqual(
            len(gateway.background_calls),
            2,
            f"checkpoint storm: {len(gateway.background_calls)} background turns",
        )

    async def test_a_person_turn_clears_the_suppression(self) -> None:
        """Requirement: background resumes once something meaningful changes."""
        source = self._Source(6)
        typed = asyncio.Queue()
        gateway = FakeGateway(
            (
                outcome(
                    GoalStatus.ACTIVE,
                    None,
                    reason="budget_exceeded",
                    core_state=CoreState.CHECKPOINTED,
                ),
            )
            + tuple(outcome(GoalStatus.ACTIVE, "answer") for _ in range(12))
        )
        session = VoiceSession(
            gateway,
            self._OpenTranscriber(),
            FakeSynthesizer(),
            "friedl",
            8,
            3650,
            clock=lambda: NOW,
            event_source=source,
        )
        # Friedl types while the backlog is still queued, which is the live
        # sequence. The first background turn checkpoints on the budget and
        # engages suppression; his line must still be taken, and background
        # work must resume once it has been.
        iterator = session.exchange(
            "conversation-1", incoming_audio(), typed=typed
        )
        person_queued = False
        for _ in range(60):
            try:
                await asyncio.wait_for(iterator.__anext__(), timeout=0.5)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                break
            if gateway.background_calls and not person_queued:
                await typed.put("Hey ALX!")
                person_queued = True
            if gateway.calls and len(gateway.background_calls) >= 2:
                break

        self.assertEqual(len(gateway.calls), 1, "the person turn did not run")
        self.assertGreaterEqual(
            len(gateway.background_calls),
            1,
            "background work never resumed after the person turn",
        )
