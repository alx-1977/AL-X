"""Cartesia speaks as ElevenLabs does; one setting picks the provider (19 Nov 2026)."""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from alx.bootstrap import build_runtime_providers  # noqa: E402
from alx.config import RuntimeSettings  # noqa: E402
from alx.providers.cartesia_tts import CartesiaSynthesizer, cartesia_output_format  # noqa: E402
from alx.providers.elevenlabs import ElevenLabsSynthesizer  # noqa: E402
from alx.providers.errors import ProviderError  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_runtime_config import environment  # noqa: E402


def cartesia_env(**changes: str) -> dict[str, str]:
    values = environment(ALX_TTS_PROVIDER="cartesia", CARTESIA_API_KEY="cartesia-secret",
                         CARTESIA_VOICE_ID="cartesia-voice")
    # ElevenLabs-only settings stay in his .env; Cartesia must not use them.
    values.pop("ALX_TTS_API_KEY")
    values.pop("ALX_TTS_VOICE_ID")
    values.update(changes)
    return values


def synthesizer(handler, sink=None) -> CartesiaSynthesizer:
    return CartesiaSynthesizer("sonic-2", "key", "voice-1", "https://api.cartesia.ai", "2025-04-16",
                               "mp3_44100_128", 30, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                               telemetry_sink=sink)


class CartesiaSpeechTests(unittest.TestCase):
    def test_streams_audio_with_the_request_cartesia_expects(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["headers"] = request.headers
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, content=b"mp3-bytes")

        events = []
        chunks = asyncio.run(self._collect(synthesizer(handler, lambda _c, e: events.append(e["code"]))))
        self.assertEqual(seen["url"], "https://api.cartesia.ai/tts/bytes")
        self.assertEqual(seen["headers"]["X-API-Key"], "key")
        self.assertEqual(seen["headers"]["Cartesia-Version"], "2025-04-16")
        self.assertEqual(seen["body"]["voice"], {"mode": "id", "id": "voice-1"})
        self.assertEqual(seen["body"]["output_format"], {"container": "mp3", "sample_rate": 44100, "bit_rate": 128000})
        self.assertEqual(seen["body"]["transcript"], "Hello Friedl")
        self.assertEqual([c.payload for c in chunks], [b"mp3-bytes", b""])
        self.assertTrue(chunks[-1].final)
        self.assertEqual(chunks[0].media_type, "audio/mpeg")
        self.assertEqual(events, ["tts.request_sent", "tts.stream_connected", "tts.first_audio_byte"])

    def test_refusal_reports_its_cause_and_never_the_body(self) -> None:
        events = []
        refusing = synthesizer(lambda request: httpx.Response(402, content=b'{"error":"Hello Friedl"}'),
                               lambda _c, e: events.append(e))
        with self.assertRaises(ProviderError) as raised:
            asyncio.run(self._collect(refusing))
        self.assertEqual(raised.exception.reason, "quota_exceeded")
        self.assertEqual(events[-1]["code"], "tts.refused")
        self.assertEqual(events[-1]["status_code"], 402)
        self.assertNotIn("Hello Friedl", json.dumps(events) + str(raised.exception))

    def test_output_format_follows_the_shared_setting(self) -> None:
        self.assertEqual(cartesia_output_format("pcm_16000"),
                         {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 16000})
        with self.assertRaises(ValueError):
            cartesia_output_format("opus_48000_64")

    def test_one_setting_switches_the_provider(self) -> None:
        settings = RuntimeSettings.from_environment(cartesia_env(ALX_STT_API_VERSION="2025-04-16"))
        speech = settings.text_to_speech
        self.assertEqual((speech.provider, speech.model, speech.api_key, speech.voice_id, speech.api_version),
                         ("cartesia", "sonic-2", "cartesia-secret", "cartesia-voice", "2025-04-16"))
        with mock.patch("alx.bootstrap.providers.subscription_cli_present", return_value=True):
            self.assertIsInstance(build_runtime_providers(settings).text_to_speech, CartesiaSynthesizer)
            back = RuntimeSettings.from_environment(environment())
            self.assertIsInstance(build_runtime_providers(back).text_to_speech, ElevenLabsSynthesizer)

    def test_cartesia_needs_no_elevenlabs_dictionary(self) -> None:
        values = cartesia_env()
        values.pop("ALX_TTS_PRONUNCIATION_DICTIONARY_ID")
        values.pop("ALX_TTS_PRONUNCIATION_DICTIONARY_VERSION_ID")
        self.assertEqual(RuntimeSettings.from_environment(values).text_to_speech.provider, "cartesia")

    @staticmethod
    async def _collect(synth: CartesiaSynthesizer):
        return [chunk async for chunk in synth.synthesize("Hello Friedl", "turn-1")]


if __name__ == "__main__":
    unittest.main()
