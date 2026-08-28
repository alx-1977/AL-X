"""Cartesia realtime transcription; audio becomes text but never intent."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterable, AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import websockets

from alx.contracts import AudioChunk, TranscriptionEvent, TranscriptionState
from alx.providers.errors import ProviderError


class CartesiaTranscriber:
    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str,
        api_version: str,
        encoding: str,
        sample_rate_hz: int,
        turn_start_threshold: float,
        turn_eager_end_threshold: float,
        turn_end_threshold: float,
        turn_end_timeout_ms: int,
        connection_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._api_version = api_version
        self._encoding = encoding
        self._sample_rate_hz = sample_rate_hz
        self._turn_start_threshold = turn_start_threshold
        self._turn_eager_end_threshold = turn_eager_end_threshold
        self._turn_end_threshold = turn_end_threshold
        self._turn_end_timeout_ms = turn_end_timeout_ms
        self._connect = connection_factory or websockets.connect

    async def transcribe(
        self,
        chunks: AsyncIterable[AudioChunk],
    ) -> AsyncIterator[TranscriptionEvent]:
        query = urlencode(
            {
                "model": self._model,
                "encoding": self._encoding,
                "sample_rate": self._sample_rate_hz,
                "cartesia_version": self._api_version,
                "turn_start_threshold": self._turn_start_threshold,
                "turn_eager_end_threshold": self._turn_eager_end_threshold,
                "turn_end_threshold": self._turn_end_threshold,
                "turn_end_timeout_ms": self._turn_end_timeout_ms,
            }
        )
        endpoint = f"{self._base_url}/stt/turns/websocket?{query}"
        try:
            async with self._connect(
                endpoint,
                additional_headers={"X-API-Key": self._api_key},
            ) as socket:
                sender = asyncio.create_task(self._send_audio(socket, chunks))
                event_number = 0
                try:
                    async for raw_event in socket:
                        event_number += 1
                        event = self._parse_event(raw_event, event_number)
                        if event is not None:
                            yield event
                finally:
                    await sender
        except ProviderError:
            raise
        except Exception as error:
            raise ProviderError("cartesia", type(error).__name__) from error

    async def _send_audio(self, socket: Any, chunks: AsyncIterable[AudioChunk]) -> None:
        async for chunk in chunks:
            if chunk.sample_rate_hz not in (None, self._sample_rate_hz):
                raise ProviderError("cartesia", "sample_rate_mismatch")
            if chunk.payload:
                await socket.send(chunk.payload)
        await socket.send(json.dumps({"type": "close"}))

    @staticmethod
    def _parse_event(raw_event: str | bytes, event_number: int) -> TranscriptionEvent | None:
        if isinstance(raw_event, bytes):
            raise ProviderError("cartesia", "unexpected_binary_event")
        try:
            body = json.loads(raw_event)
        except json.JSONDecodeError as error:
            raise ProviderError("cartesia", "invalid_event") from error
        event_type = body.get("type")
        if event_type == "error":
            raise ProviderError("cartesia", "remote_error")
        if event_type not in ("turn.update", "turn.eager_end", "turn.end"):
            return None
        content = body.get("transcript")
        request_id = body.get("request_id")
        if not isinstance(content, str) or not content.strip() or not isinstance(request_id, str):
            raise ProviderError("cartesia", "invalid_transcription_event")
        state = (
            TranscriptionState.FINAL
            if event_type == "turn.end"
            else TranscriptionState.PARTIAL
        )
        return TranscriptionEvent(
            request_id,
            f"{request_id}-{event_number}",
            state,
            content,
            datetime.now(UTC),
            {"provider_event": event_type},
        )
