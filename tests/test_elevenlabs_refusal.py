"""ElevenLabs' status code for a refused request reaches the log and console (19 Nov 2026)."""

import asyncio
import json
import unittest

import httpx

from alx.providers.elevenlabs import ElevenLabsSynthesizer, _refusal
from alx.providers.errors import ProviderError


class RefusalReasonTests(unittest.TestCase):
    def test_only_the_status_code_is_kept(self) -> None:
        body = b'{"detail": {"status": "quota_exceeded", "message": "You have 1 credits remaining."}}'
        self.assertEqual(_refusal(body), "quota_exceeded")
        self.assertEqual(_refusal(b'{"detail": "Invalid API key"}'), "")
        self.assertEqual(_refusal(b"Unauthorized"), "")
        self.assertEqual(_refusal(b'{"detail": {"status": "Quote this text back"}}'), "")

    def test_refusal_raises_with_its_cause_and_telemetry(self) -> None:
        body = b'{"detail": {"status": "quota_exceeded", "message": "Hello Friedl"}}'
        events = []
        synth = ElevenLabsSynthesizer(
            "model", "key", "voice", "https://api.elevenlabs.io", "mp3_44100_128", 30, "dict", "ver",
            client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401, content=body))),
            telemetry_sink=lambda _c, e: events.append(e))

        async def run():
            return [c async for c in synth.synthesize("Hello Friedl", "turn-1")]
        with self.assertRaises(ProviderError) as raised:
            asyncio.run(run())
        self.assertEqual(raised.exception.reason, "quota_exceeded")
        self.assertEqual((events[-1]["code"], events[-1]["reason"], events[-1]["status_code"]),
                         ("tts.refused", "quota_exceeded", 401))
        self.assertNotIn("Hello Friedl", json.dumps(events) + str(raised.exception))


if __name__ == "__main__":
    unittest.main()
