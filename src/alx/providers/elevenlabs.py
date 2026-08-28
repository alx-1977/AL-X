"""ElevenLabs streaming synthesis for AL/X's authoritative response."""

from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import quote
from uuid import uuid4

import httpx

from alx.contracts import AudioChunk
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
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._voice_id = voice_id
        self._base_url = base_url.rstrip("/")
        self._output_format = output_format
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def synthesize(self, response: str) -> AsyncIterator[AudioChunk]:
        if not response.strip():
            raise ValueError("response must not be blank")
        stream_id = str(uuid4())
        media_type = _media_type(self._output_format)
        endpoint = (
            f"{self._base_url}/v1/text-to-speech/"
            f"{quote(self._voice_id, safe='')}/stream"
        )
        try:
            async with self._client.stream(
                "POST",
                endpoint,
                params={"output_format": self._output_format},
                headers={
                    "xi-api-key": self._api_key,
                    "Content-Type": "application/json",
                },
                json={"text": response, "model_id": self._model},
            ) as provider_response:
                provider_response.raise_for_status()
                sequence = 0
                async for payload in provider_response.aiter_bytes():
                    if payload:
                        yield AudioChunk(stream_id, sequence, payload, media_type)
                        sequence += 1
                yield AudioChunk(stream_id, sequence, b"", media_type, final=True)
        except httpx.HTTPError as error:
            raise ProviderError("elevenlabs", type(error).__name__) from error
