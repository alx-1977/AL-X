from __future__ import annotations

import json
import unittest
from urllib.parse import parse_qs

import httpx

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    AudioChunk,
    ModelMessage,
    ModelRequest,
    ModelRole,
    TranscriptionState,
)
from alx.providers import (  # noqa: E402
    CartesiaTranscriber,
    ElevenLabsSynthesizer,
    XAIReasoningModel,
)
from alx.providers.errors import ProviderError  # noqa: E402


class XAIAdapterTests(unittest.TestCase):
    def test_structured_request_and_response_stay_provider_neutral(self) -> None:
        captured = {}

        def respond(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            return httpx.Response(
                200,
                json={
                    "model": "configured-model-version",
                    "choices": [
                        {"message": {"content": json.dumps({"response": "hello"})}}
                    ],
                    "usage": {"total_tokens": 7},
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(respond))
        adapter = XAIReasoningModel(
            "configured-model", "secret", "https://model.example", 10, client
        )
        request = ModelRequest(
            (ModelMessage(ModelRole.USER, "natural request"),),
            "alx_decision",
            {
                "type": "object",
                "properties": {"response": {"type": "string"}},
                "required": ("response",),
                "additionalProperties": False,
            },
        )

        result = adapter.complete(request)

        sent = json.loads(captured["request"].content)
        self.assertEqual(captured["request"].url.path, "/v1/chat/completions")
        self.assertEqual(sent["model"], "configured-model")
        self.assertEqual(sent["response_format"]["type"], "json_schema")
        self.assertEqual(sent["messages"][0]["content"], "natural request")
        self.assertEqual(result.provider, "xai")
        self.assertEqual(result.model, "configured-model-version")
        self.assertEqual(result.output["response"], "hello")

    def test_failure_is_sanitised(self) -> None:
        def fail(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="remote body must not escape")

        client = httpx.Client(transport=httpx.MockTransport(fail))
        adapter = XAIReasoningModel("model", "very-secret", "https://example", 10, client)
        request = ModelRequest(
            (ModelMessage(ModelRole.USER, "request"),),
            "result",
            {"type": "object"},
        )
        with self.assertRaises(ProviderError) as caught:
            adapter.complete(request)
        self.assertNotIn("very-secret", str(caught.exception))
        self.assertNotIn("remote body", str(caught.exception))


class FakeSocket:
    def __init__(self, events):
        self.events = iter(events)
        self.sent = []

    async def send(self, value):
        self.sent.append(value)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.events)
        except StopIteration:
            raise StopAsyncIteration


class FakeConnection:
    def __init__(self, socket, capture):
        self.socket = socket
        self.capture = capture

    def __call__(self, endpoint, **options):
        self.capture["endpoint"] = endpoint
        self.capture["options"] = options
        return self

    async def __aenter__(self):
        return self.socket

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class SpeechAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_cartesia_sends_only_audio_and_returns_transcription_events(self) -> None:
        capture = {}
        socket = FakeSocket(
            (
                json.dumps({"type": "connected", "request_id": "request-1"}),
                json.dumps(
                    {
                        "type": "turn.update",
                        "request_id": "request-1",
                        "transcript": "Good morning",
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.end",
                        "request_id": "request-1",
                        "transcript": "Good morning ALX",
                    }
                ),
            )
        )
        adapter = CartesiaTranscriber(
            "configured-stt",
            "secret",
            "wss://speech.example",
            "configured-version",
            "pcm_s16le",
            16000,
            0.7,
            0.5,
            0.4,
            4500,
            FakeConnection(socket, capture),
        )

        async def audio():
            yield AudioChunk("audio-1", 0, b"one", "audio/pcm", 16000)
            yield AudioChunk("audio-1", 1, b"two", "audio/pcm", 16000)

        events = [event async for event in adapter.transcribe(audio())]

        query = parse_qs(capture["endpoint"].split("?", 1)[1])
        self.assertEqual(query["model"], ["configured-stt"])
        self.assertEqual(query["turn_start_threshold"], ["0.7"])
        self.assertEqual(query["turn_eager_end_threshold"], ["0.5"])
        self.assertEqual(query["turn_end_threshold"], ["0.4"])
        self.assertEqual(query["turn_end_timeout_ms"], ["4500"])
        self.assertEqual(capture["options"]["additional_headers"], {"X-API-Key": "secret"})
        self.assertEqual(socket.sent[:2], [b"one", b"two"])
        self.assertEqual(json.loads(socket.sent[2]), {"type": "close"})
        self.assertEqual(events[0].state, TranscriptionState.PARTIAL)
        self.assertEqual(events[1].state, TranscriptionState.FINAL)
        self.assertEqual(events[1].content, "Good morning ALX")
        self.assertNotIn("intent", events[1].acoustic_metadata)

    async def test_elevenlabs_streams_only_authoritative_response_audio(self) -> None:
        captured = {}

        async def respond(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            return httpx.Response(200, content=b"spoken-audio")

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        adapter = ElevenLabsSynthesizer(
            "configured-tts",
            "secret",
            "preferred-voice",
            "https://speech.example",
            "mp3_44100_128",
            10,
            client,
        )

        chunks = [chunk async for chunk in adapter.synthesize("ALX response")]

        sent = json.loads(captured["request"].content)
        self.assertIn("preferred-voice", captured["request"].url.path)
        self.assertEqual(sent, {"text": "ALX response", "model_id": "configured-tts"})
        self.assertEqual(chunks[0].payload, b"spoken-audio")
        self.assertTrue(chunks[-1].final)
        self.assertEqual(chunks[-1].payload, b"")
        await client.aclose()


if __name__ == "__main__":
    unittest.main()
