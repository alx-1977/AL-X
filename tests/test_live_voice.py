from __future__ import annotations

import tempfile
import unittest
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.bootstrap.live_voice import (  # noqa: E402
    load_environment, locate_active_goal, migrate_legacy_conversations,
)
from alx.contracts import (  # noqa: E402
    AudioChunk,
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
from alx.core import CoreState  # noqa: E402
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

    def receive_conversation_turn(self, turn, step_budget, retention_until):
        self.calls.append((turn, step_budget, retention_until))
        return next(self.outcomes)


def outcome(
    status,
    response="authoritative response",
    reason=None,
    core_state=CoreState.RESPONDED,
):
    return SimpleNamespace(
        state=core_state,
        snapshot=SimpleNamespace(state=SimpleNamespace(status=status)),
        response=response,
        reason=reason,
    )


def transcription(identifier, state, content):
    return TranscriptionEvent("stt", identifier, state, content, NOW)


async def incoming_audio():
    yield AudioChunk("mic", 0, b"pcm", "audio/pcm", 16000)


class VoiceSessionTests(unittest.IsolatedAsyncioTestCase):
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
        self.assertEqual(
            [event.kind for event in events],
            [
                VoiceEventKind.HEARING,
                VoiceEventKind.THINKING,
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
                VoiceEventKind.SPEAKING,
                VoiceEventKind.AUDIO,
                VoiceEventKind.AUDIO,
                VoiceEventKind.LISTENING,
            ],
        )
        self.assertEqual(events[1].reason, "decision_invalid")
        self.assertEqual(synthesizer.responses, ["recovered response"])


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

    def test_active_goal_is_recovered_from_latest_durable_conversation_state(self) -> None:
        older = SimpleNamespace(
            state=SimpleNamespace(goal_id="older", status=GoalStatus.AWAITING_INPUT),
            conversation_id="conversation-1",
        )
        newer = SimpleNamespace(
            state=SimpleNamespace(goal_id="newer", status=GoalStatus.ACTIVE),
            conversation_id="conversation-1",
        )
        completed = SimpleNamespace(
            state=SimpleNamespace(goal_id="done", status=GoalStatus.COMPLETED),
            conversation_id="conversation-1",
        )
        store = SimpleNamespace(list_goals=lambda: (older, newer, completed))

        self.assertEqual(locate_active_goal(store, "conversation-1"), "newer")
        self.assertIsNone(locate_active_goal(store, "another-conversation"))

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
