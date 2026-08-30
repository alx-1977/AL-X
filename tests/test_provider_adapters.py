from __future__ import annotations

import json
import ssl
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
    OpenAIReasoningModel,
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
            "configured-model",
            "secret",
            "https://model.example",
            10,
            client,
            streaming=False,
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

    def test_streaming_reports_safe_latency_usage_and_cache_affinity(self) -> None:
        captured = {}
        telemetry = []

        def respond(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            events = (
                {
                    "model": "configured-model-version",
                    "service_tier": "priority",
                    "choices": [{"delta": {"content": "{\"response\":"}}],
                },
                {
                    "model": "configured-model-version",
                    "service_tier": "priority",
                    "choices": [{"delta": {"content": "\"hello\"}"}}],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 25,
                        "total_tokens": 125,
                        "prompt_tokens_details": {"cached_tokens": 80},
                        "completion_tokens_details": {"reasoning_tokens": 20},
                    },
                },
            )
            body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
            return httpx.Response(200, text=body + "data: [DONE]\n\n")

        client = httpx.Client(transport=httpx.MockTransport(respond))
        adapter = XAIReasoningModel(
            "configured-model",
            "secret",
            "https://model.example",
            10,
            client,
            streaming=True,
            service_tier="priority",
            telemetry_sink=lambda key, values: telemetry.append((key, values)),
        )
        request = ModelRequest(
            (ModelMessage(ModelRole.USER, "natural request"),),
            "result",
            {"type": "object"},
            "conversation-1",
        )

        result = adapter.complete(request)

        sent = json.loads(captured["request"].content)
        self.assertTrue(sent["stream"])
        self.assertEqual(sent["service_tier"], "priority")
        self.assertEqual(captured["request"].headers["x-grok-conv-id"], "conversation-1")
        self.assertEqual(result.output["response"], "hello")
        self.assertEqual(telemetry[0][0], "conversation-1")
        self.assertEqual(telemetry[0][1]["cached_tokens"], 80)
        self.assertEqual(telemetry[0][1]["reasoning_tokens"], 20)
        self.assertEqual(telemetry[0][1]["service_tier"], "priority")

    def test_failure_is_sanitised(self) -> None:
        def fail(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="remote body must not escape")

        client = httpx.Client(transport=httpx.MockTransport(fail))
        adapter = XAIReasoningModel(
            "model", "very-secret", "https://example", 10, client, streaming=False
        )
        request = ModelRequest(
            (ModelMessage(ModelRole.USER, "request"),),
            "result",
            {"type": "object"},
        )
        with self.assertRaises(ProviderError) as caught:
            adapter.complete(request)
        self.assertNotIn("very-secret", str(caught.exception))
        self.assertNotIn("remote body", str(caught.exception))

class OpenAIAdapterTests(unittest.TestCase):
    def test_responses_request_preserves_neutral_structured_contract(self) -> None:
        captured = {}

        def respond(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            return httpx.Response(
                200,
                json={
                    "model": "configured-model-version",
                    "service_tier": "priority",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": json.dumps({"response": "hello"}),
                                }
                            ],
                        }
                    ],
                    "usage": {"total_tokens": 7},
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(respond))
        adapter = OpenAIReasoningModel(
            "configured-model",
            "secret",
            "https://model.example",
            10,
            client,
            streaming=False,
            service_tier="priority",
            reasoning_effort="medium",
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
            "conversation-1",
        )

        result = adapter.complete(request)

        sent = json.loads(captured["request"].content)
        self.assertEqual(captured["request"].url.path, "/v1/responses")
        self.assertEqual(sent["model"], "configured-model")
        self.assertEqual(sent["input"][0]["content"], "natural request")
        self.assertEqual(sent["text"]["format"]["type"], "json_schema")
        self.assertEqual(sent["reasoning"]["effort"], "medium")
        self.assertEqual(sent["prompt_cache_key"], "conversation-1")
        self.assertEqual(sent["service_tier"], "priority")
        self.assertEqual(result.provider, "openai")
        self.assertEqual(result.model, "configured-model-version")
        self.assertEqual(result.output["response"], "hello")

    def test_streaming_reports_latency_usage_and_reasoning(self) -> None:
        captured = {}
        telemetry = []

        def respond(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            events = (
                {"type": "response.created"},
                {"type": "response.output_text.delta", "delta": "{\"response\":"},
                {"type": "response.output_text.delta", "delta": "\"hello\"}"},
                {
                    "type": "response.completed",
                    "response": {
                        "model": "configured-model-version",
                        "service_tier": "priority",
                        "output": [],
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 25,
                            "total_tokens": 125,
                            "input_tokens_details": {"cached_tokens": 80},
                            "output_tokens_details": {"reasoning_tokens": 20},
                        },
                    },
                },
            )
            body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
            return httpx.Response(200, text=body)

        client = httpx.Client(transport=httpx.MockTransport(respond))
        adapter = OpenAIReasoningModel(
            "configured-model",
            "secret",
            "https://model.example",
            10,
            client,
            streaming=True,
            service_tier="priority",
            reasoning_effort="high",
            telemetry_sink=lambda key, values: telemetry.append((key, values)),
        )
        request = ModelRequest(
            (ModelMessage(ModelRole.USER, "natural request"),),
            "result",
            {"type": "object"},
            "conversation-1",
        )

        result = adapter.complete(request)

        sent = json.loads(captured["request"].content)
        self.assertTrue(sent["stream"])
        self.assertFalse(sent["stream_options"]["include_obfuscation"])
        self.assertEqual(result.output["response"], "hello")
        self.assertEqual(telemetry[0][0], "conversation-1")
        self.assertEqual(telemetry[0][1]["cached_tokens"], 80)
        self.assertEqual(telemetry[0][1]["reasoning_tokens"], 20)
        self.assertEqual(telemetry[0][1]["reasoning_effort"], "high")
        self.assertEqual(telemetry[0][1]["service_tier"], "priority")

    def test_failure_is_sanitised(self) -> None:
        def fail(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="remote body must not escape")

        client = httpx.Client(transport=httpx.MockTransport(fail))
        adapter = OpenAIReasoningModel(
            "model", "very-secret", "https://example", 10, client, streaming=False
        )
        request = ModelRequest(
            (ModelMessage(ModelRole.USER, "request"),),
            "result",
            {"type": "object"},
        )
        with self.assertRaises(ProviderError) as caught:
            adapter.complete(request)
        self.assertNotIn("very-secret", str(caught.exception))
        self.assertNotIn("remote body", str(caught.exception))

    def test_stream_failure_reports_only_sanitized_structural_code(self) -> None:
        telemetry = []

        def fail(request: httpx.Request) -> httpx.Response:
            event = {"type": "response.incomplete", "private": "must not escape"}
            return httpx.Response(200, text=f"data: {json.dumps(event)}\n\n")

        client = httpx.Client(transport=httpx.MockTransport(fail))
        adapter = OpenAIReasoningModel(
            "model", "very-secret", "https://example", 10, client,
            streaming=True,
            telemetry_sink=lambda key, values: telemetry.append(values),
        )
        request = ModelRequest(
            (ModelMessage(ModelRole.USER, "private request"),),
            "result", {"type": "object"}, "conversation-1",
        )
        with self.assertRaises(ProviderError) as caught:
            adapter.complete(request)
        self.assertEqual(caught.exception.reason, "response_incomplete")
        self.assertEqual(telemetry[0]["error_code"], "response_incomplete")
        self.assertNotIn("private", str(caught.exception))
        self.assertNotIn("very-secret", str(caught.exception))

    def test_failed_stream_exposes_code_but_never_private_message(self) -> None:
        def fail(request: httpx.Request) -> httpx.Response:
            event = {
                "type": "response.failed",
                "response": {
                    "error": {
                        "code": "server_error",
                        "message": "private provider detail must not escape",
                    }
                },
            }
            return httpx.Response(200, text=f"data: {json.dumps(event)}\n\n")

        client = httpx.Client(transport=httpx.MockTransport(fail))
        adapter = OpenAIReasoningModel(
            "model", "very-secret", "https://example", 10, client,
            streaming=True,
        )
        request = ModelRequest(
            (ModelMessage(ModelRole.USER, "private request"),),
            "result", {"type": "object"}, "conversation-1",
        )
        with self.assertRaises(ProviderError) as caught:
            adapter.complete(request)
        self.assertEqual(
            caught.exception.reason, "response_failed_server_error"
        )
        self.assertNotIn("provider detail", str(caught.exception))
        self.assertNotIn("very-secret", str(caught.exception))


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
        self.assertEqual(capture["options"]["ssl"].verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(socket.sent[:2], [b"one", b"two"])
        self.assertEqual(json.loads(socket.sent[2]), {"type": "close"})
        self.assertEqual(events[0].state, TranscriptionState.PARTIAL)
        self.assertEqual(events[1].state, TranscriptionState.FINAL)
        self.assertEqual(events[1].content, "Good morning ALX")
        self.assertNotIn("intent", events[1].acoustic_metadata)

    async def test_elevenlabs_streams_only_authoritative_response_audio(self) -> None:
        captured = {}
        telemetry = []

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
            "dictionary-id",
            "dictionary-version-id",
            client,
            telemetry_sink=lambda key, values: telemetry.append((key, values)),
        )

        chunks = [
            chunk
            async for chunk in adapter.synthesize("ALX response", "conversation-1")
        ]

        sent = json.loads(captured["request"].content)
        self.assertIn("preferred-voice", captured["request"].url.path)
        self.assertEqual(
            sent,
            {
                "text": "ALX response",
            "model_id": "configured-tts",
            "apply_text_normalization": "on",
            "voice_settings": {
                "speed": 1.0,
                "stability": 0.5,
                "similarity_boost": 0.75,
                "use_speaker_boost": True,
            },
            "pronunciation_dictionary_locators": [
                    {
                        "pronunciation_dictionary_id": "dictionary-id",
                        "version_id": "dictionary-version-id",
                    }
                ],
            },
        )
        self.assertEqual(chunks[0].payload, b"spoken-audio")
        self.assertTrue(chunks[-1].final)
        self.assertEqual(chunks[-1].payload, b"")
        self.assertEqual(
            [item[1]["code"] for item in telemetry],
            [
                "tts.request_sent",
                "tts.text_sent",
                "tts.stream_connected",
                "tts.first_audio_byte",
            ],
        )
        self.assertTrue(all(item[0] == "conversation-1" for item in telemetry))
        self.assertTrue(all(item[1]["transport"] == "http" for item in telemetry))
        await client.aclose()

    async def test_elevenlabs_does_not_interpret_compact_r_number_forms(self) -> None:
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
            "dictionary-id",
            "dictionary-version-id",
            client,
        )
        authoritative_response = (
            "Check resistors R5, R10, and R100. "
            "The quote is R2000, R2,000, or R2 000.50."
        )

        _ = [chunk async for chunk in adapter.synthesize(authoritative_response)]

        sent = json.loads(captured["request"].content)
        self.assertEqual(sent["text"], authoritative_response)
        await client.aclose()

    async def test_elevenlabs_sends_configured_voice_speed(self) -> None:
        captured = {}

        async def respond(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            return httpx.Response(200, content=b"spoken-audio")

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        adapter = ElevenLabsSynthesizer(
            "configured-tts", "secret", "preferred-voice",
            "https://speech.example", "mp3_44100_128", 10,
            "dictionary-id", "dictionary-version-id", client,
            speed=1.15,
        )
        _ = [chunk async for chunk in adapter.synthesize("ALX response")]
        sent = json.loads(captured["request"].content)
        # Sending voice_settings replaces the whole object, so every field the
        # voice relies on must be present, not only the one being configured.
        self.assertEqual(
            sent["voice_settings"],
            {
                "speed": 1.15,
                "stability": 0.5,
                "similarity_boost": 0.75,
                "use_speaker_boost": True,
            },
        )
        await client.aclose()


if __name__ == "__main__":
    unittest.main()
