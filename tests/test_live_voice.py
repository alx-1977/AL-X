from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.bootstrap.live_voice import load_environment, locate_active_goal  # noqa: E402
from alx.contracts import (  # noqa: E402
    AudioChunk,
    ConversationOrigin,
    GoalStatus,
    TranscriptionEvent,
    TranscriptionState,
)
from alx.interfaces import VoiceEventKind, VoiceSession  # noqa: E402


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

    async def synthesize(self, response):
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


def outcome(status, response="authoritative response", reason=None):
    return SimpleNamespace(
        snapshot=SimpleNamespace(state=SimpleNamespace(status=status)),
        response=response,
        reason=reason,
    )


def transcription(identifier, state, content):
    return TranscriptionEvent("stt", identifier, state, content, NOW)


async def incoming_audio():
    yield AudioChunk("mic", 0, b"pcm", "audio/pcm", 16000)


class VoiceSessionTests(unittest.IsolatedAsyncioTestCase):
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


class BootstrapVoiceTests(unittest.TestCase):
    def test_active_goal_is_recovered_from_latest_durable_conversation_state(self) -> None:
        older = SimpleNamespace(
            state=SimpleNamespace(goal_id="older", status=GoalStatus.AWAITING_INPUT),
            turns=(SimpleNamespace(conversation_id="conversation-1", occurred_at=NOW),),
        )
        newer = SimpleNamespace(
            state=SimpleNamespace(goal_id="newer", status=GoalStatus.ACTIVE),
            turns=(
                SimpleNamespace(
                    conversation_id="conversation-1",
                    occurred_at=NOW.replace(minute=31),
                ),
            ),
        )
        completed = SimpleNamespace(
            state=SimpleNamespace(goal_id="done", status=GoalStatus.COMPLETED),
            turns=(
                SimpleNamespace(
                    conversation_id="conversation-1",
                    occurred_at=NOW.replace(minute=32),
                ),
            ),
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
