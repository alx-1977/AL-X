"""Generate and transcribe the pronunciation fixture with the configured voice."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import wave

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alx.bootstrap.live_voice import load_environment  # noqa: E402
from alx.config import RuntimeSettings  # noqa: E402
from alx.providers import ElevenLabsSynthesizer  # noqa: E402


async def main() -> None:
    environment = load_environment(ROOT / ".env")
    settings = RuntimeSettings.from_environment(environment)
    fixture = json.loads(
        (ROOT / "tests/fixtures/pronunciation_acceptance.json").read_text(
            encoding="utf-8"
        )
    )
    synthesizer = ElevenLabsSynthesizer(
        settings.text_to_speech.model,
        settings.text_to_speech.api_key,
        settings.text_to_speech.voice_id,
        settings.text_to_speech.base_url,
        "pcm_16000",
        settings.text_to_speech.timeout_seconds,
        settings.text_to_speech.pronunciation_dictionary_id,
        settings.text_to_speech.pronunciation_dictionary_version_id,
    )
    observations = []
    async with httpx.AsyncClient(
        timeout=settings.text_to_speech.timeout_seconds
    ) as client:
        for case in fixture["cases"]:
            chunks = [chunk async for chunk in synthesizer.synthesize(case["written"])]
            with tempfile.NamedTemporaryFile(suffix=".wav") as temporary:
                with wave.open(temporary.name, "wb") as audio_file:
                    audio_file.setnchannels(1)
                    audio_file.setsampwidth(2)
                    audio_file.setframerate(16000)
                    audio_file.writeframes(
                        b"".join(chunk.payload for chunk in chunks if chunk.payload)
                    )
                with open(temporary.name, "rb") as audio_file:
                    response = await client.post(
                        f"{settings.text_to_speech.base_url}/v1/speech-to-text",
                        headers={"xi-api-key": settings.text_to_speech.api_key},
                        data={"model_id": "scribe_v2"},
                        files={"file": ("acceptance.wav", audio_file, "audio/wav")},
                    )
                response.raise_for_status()
            observations.append(
                {
                    "category": case["category"],
                    "written": case["written"],
                    "expected_spoken": case["expected_spoken"],
                    "observed_transcript": response.json()["text"],
                }
            )
    print(json.dumps({"vocabulary_version": fixture["vocabulary_version"], "observations": observations}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
