"""ElevenLabs streaming synthesis for AL/X's authoritative response."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from time import monotonic
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx

from alx.contracts import AudioChunk
from alx.providers.elevenlabs_pronunciation import DictionaryLocator
from alx.providers.errors import ProviderError


def _media_type(output_format: str) -> str:
    codec = output_format.split("_", 1)[0]
    return {
        "mp3": "audio/mpeg",
        "opus": "audio/opus",
        "pcm": "audio/pcm",
        "ulaw": "audio/basic",
        "alaw": "audio/basic",
    }.get(codec, "application/octet-stream")


class ElevenLabsSynthesizer:
    def __init__(
        self,
        model: str,
        api_key: str,
        voice_id: str,
        base_url: str,
        output_format: str,
        timeout_seconds: int,
        pronunciation_dictionary_id: str,
        pronunciation_dictionary_version_id: str,
        client: httpx.AsyncClient | None = None,
        telemetry_sink: Callable[[str, Mapping[str, Any]], None] | None = None,
        speed: float = 1.0,
        stability: float = 0.5,
        similarity_boost: float = 0.75,
        speaker_boost: bool = True,
    ) -> None:
        if not 0.7 <= speed <= 1.2:
            raise ValueError("ElevenLabs speed must be between 0.7 and 1.2")
        for name, value in (
            ("stability", stability), ("similarity_boost", similarity_boost)
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"ElevenLabs {name} must be between 0.0 and 1.0")
        self._model = model
        self._api_key = api_key
        self._voice_id = voice_id
        self._base_url = base_url.rstrip("/")
        self._output_format = output_format
        self._pronunciation_dictionary = DictionaryLocator(
            pronunciation_dictionary_id,
            pronunciation_dictionary_version_id,
        )
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._telemetry_sink = telemetry_sink
        self._speed = speed
        # Sending voice_settings replaces the whole object, so every field the
        # voice relies on must be supplied. Omitting them fell back to API
        # defaults rather than the voice's own, which let short utterances
        # drift in tone.
        self._stability = stability
        self._similarity_boost = similarity_boost
        self._speaker_boost = speaker_boost

    async def synthesize(
        self,
        response: str,
        correlation_id: str | None = None,
    ) -> AsyncIterator[AudioChunk]:
        if not response.strip():
            raise ValueError("response must not be blank")
        started_at = monotonic()
        stream_id = str(uuid4())
        media_type = _media_type(self._output_format)
        endpoint = (
            f"{self._base_url}/v1/text-to-speech/"
            f"{quote(self._voice_id, safe='')}/stream"
        )
        try:
            self._emit_telemetry(
                correlation_id,
                "tts.request_sent",
                started_at,
                transport="http",
            )
            self._emit_telemetry(
                correlation_id,
                "tts.text_sent",
                started_at,
                transport="http",
            )
            async with self._client.stream(
                "POST",
                endpoint,
                params={"output_format": self._output_format},
                headers={
                    "xi-api-key": self._api_key,
                    "Content-Type": "application/json",
                },
                json={
                    "text": response,
                    "model_id": self._model,
                    "apply_text_normalization": "on",
                    "voice_settings": {
                        "speed": self._speed,
                        "stability": self._stability,
                        "similarity_boost": self._similarity_boost,
                        "use_speaker_boost": self._speaker_boost,
                    },
                    "pronunciation_dictionary_locators": [
                        self._pronunciation_dictionary.as_request_value()
                    ],
                },
            ) as provider_response:
                provider_response.raise_for_status()
                self._emit_telemetry(
                    correlation_id,
                    "tts.stream_connected",
                    started_at,
                    transport="http",
                )
                sequence = 0
                async for payload in provider_response.aiter_bytes():
                    if payload:
                        if sequence == 0:
                            self._emit_telemetry(
                                correlation_id,
                                "tts.first_audio_byte",
                                started_at,
                                transport="http",
                            )
                        yield AudioChunk(stream_id, sequence, payload, media_type)
                        sequence += 1
                yield AudioChunk(stream_id, sequence, b"", media_type, final=True)
        except httpx.HTTPError as error:
            raise ProviderError("elevenlabs", type(error).__name__) from error

    def _emit_telemetry(
        self,
        correlation_id: str | None,
        code: str,
        started_at: float,
        **values: Any,
    ) -> None:
        if correlation_id is None or self._telemetry_sink is None:
            return
        try:
            self._telemetry_sink(
                correlation_id,
                {
                    "code": code,
                    "provider": "elevenlabs",
                    "model": self._model,
                    "elapsed_ms": round((monotonic() - started_at) * 1000),
                    **values,
                },
            )
        except Exception:
            return
