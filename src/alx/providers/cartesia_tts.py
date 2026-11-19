"""Cartesia streaming synthesis for AL/X's authoritative response (19 Nov 2026).

The same contract as ElevenLabsSynthesizer: her words in, audio chunks out, telemetry
codes for the console, and a sanitised ProviderError on failure. Which provider speaks
is configuration (ALX_TTS_PROVIDER); nothing above this module knows the difference.

Cartesia's /tts/bytes endpoint streams the audio as it is generated. The output format
is read from the same ALX_TTS_OUTPUT_FORMAT string ElevenLabs uses ("mp3_44100_128":
codec, sample rate, bit rate), so the browser receives the same kind of audio.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Mapping
from time import monotonic
from typing import Any
from uuid import uuid4

import httpx

from alx.contracts import AudioChunk
from alx.providers.errors import raise_provider_failure

LOGGER = logging.getLogger(__name__)

_MEDIA = {"mp3": "audio/mpeg", "wav": "audio/wav", "raw": "audio/pcm"}


def cartesia_output_format(output_format: str) -> dict[str, Any]:
    """Cartesia's output_format object from an ElevenLabs-style string.

    "mp3_44100_128" -> mp3 at 44.1 kHz, 128 kbit/s; "pcm_16000" -> raw 16-bit PCM."""
    parts = output_format.split("_")
    codec = parts[0]
    rate = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 44100
    if codec == "mp3":
        bit_rate = int(parts[2]) * 1000 if len(parts) > 2 and parts[2].isdigit() else 128000
        return {"container": "mp3", "sample_rate": rate, "bit_rate": bit_rate}
    if codec == "pcm":
        return {"container": "raw", "encoding": "pcm_s16le", "sample_rate": rate}
    raise ValueError(f"Cartesia cannot produce output format {output_format!r}")


class CartesiaSynthesizer:
    def __init__(
        self,
        model: str,
        api_key: str,
        voice_id: str,
        base_url: str,
        api_version: str,
        output_format: str,
        timeout_seconds: int,
        client: httpx.AsyncClient | None = None,
        telemetry_sink: Callable[[str, Mapping[str, Any]], None] | None = None,
        language: str = "en",
    ) -> None:
        if not voice_id.strip():
            raise ValueError("Cartesia needs a voice id (CARTESIA_VOICE_ID)")
        self._model = model
        self._api_key = api_key
        self._voice_id = voice_id
        self._base_url = base_url.rstrip("/")
        self._api_version = api_version
        self._format = cartesia_output_format(output_format)
        self._media_type = _MEDIA[self._format["container"]]
        self._language = language
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._telemetry_sink = telemetry_sink

    async def synthesize(
        self,
        response: str,
        correlation_id: str | None = None,
    ) -> AsyncIterator[AudioChunk]:
        if not response.strip():
            raise ValueError("response must not be blank")
        started_at = monotonic()
        stream_id = str(uuid4())
        try:
            self._emit_telemetry(correlation_id, "tts.request_sent", started_at, transport="http")
            async with self._client.stream(
                "POST",
                f"{self._base_url}/tts/bytes",
                headers={
                    "X-API-Key": self._api_key,
                    "Cartesia-Version": self._api_version,
                    "Content-Type": "application/json",
                },
                json={
                    "model_id": self._model,
                    "transcript": response,
                    "voice": {"mode": "id", "id": self._voice_id},
                    "output_format": self._format,
                    "language": self._language,
                },
            ) as provider_response:
                if provider_response.status_code >= 400:
                    # Only the status is kept: the body is untrusted and may quote the
                    # request back (errors.py). Raised outside any except block.
                    status = provider_response.status_code
                    reason = {401: "invalid_api_key", 402: "quota_exceeded",
                              429: "rate_limited"}.get(status, "http_%d" % status)
                    LOGGER.warning("Cartesia refused speech: HTTP %s %s", status, reason)
                    self._emit_telemetry(correlation_id, "tts.refused", started_at,
                                         status_code=status, reason=reason)
                    raise_provider_failure("cartesia", reason, status_code=status)
                self._emit_telemetry(correlation_id, "tts.stream_connected", started_at,
                                     transport="http")
                sequence = 0
                async for payload in provider_response.aiter_bytes():
                    if payload:
                        if sequence == 0:
                            self._emit_telemetry(correlation_id, "tts.first_audio_byte",
                                                 started_at, transport="http")
                        yield AudioChunk(stream_id, sequence, payload, self._media_type)
                        sequence += 1
                yield AudioChunk(stream_id, sequence, b"", self._media_type, final=True)
        except httpx.HTTPError as error:
            error_code = type(error).__name__
        else:
            return
        raise_provider_failure("cartesia", error_code)

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
                    "provider": "cartesia",
                    "model": self._model,
                    "elapsed_ms": round((monotonic() - started_at) * 1000),
                    **values,
                },
            )
        except Exception:
            return
