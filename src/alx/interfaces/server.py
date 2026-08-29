"""Local browser microphone and playback transport for a VoiceSession."""

from __future__ import annotations

import json
import logging
import mimetypes
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from alx.contracts import AudioChunk
from alx.interfaces.live_voice import VoiceEventKind, VoiceSession


LOGGER = logging.getLogger(__name__)

# A dropped speech transport ends one exchange, not the conversation. The person
# may simply have been silent while the provider timed the socket out.
RECOVERABLE_TRANSPORT_REASONS = frozenset({"speech_transcription_error"})


class LiveVoiceServer:
    def __init__(
        self,
        session: VoiceSession,
        host: str,
        port: int,
        sample_rate_hz: int,
        asset_root: Path,
    ) -> None:
        self._session = session
        self._host = host
        self._port = port
        self._sample_rate_hz = sample_rate_hz
        self._asset_root = asset_root.resolve()

    async def serve_forever(self) -> None:
        origins = (f"http://{self._host}:{self._port}", None)
        async with serve(
            self._handle_voice,
            self._host,
            self._port,
            origins=origins,
            process_request=self._serve_asset,
            compression=None,
            max_size=None,
        ) as server:
            await server.serve_forever()

    async def _handle_voice(self, connection: ServerConnection) -> None:
        request_path = connection.request.path if connection.request is not None else ""
        parsed = urlsplit(request_path)
        if parsed.path != "/voice":
            await connection.close(code=1008, reason="unsupported_transport_path")
            return
        conversation_id = self._conversation_id(parse_qs(parsed.query))
        LOGGER.info("Voice session connected")
        await connection.send(
            json.dumps(
                {
                    "type": "session.ready",
                    "conversation_id": conversation_id,
                    "sample_rate_hz": self._sample_rate_hz,
                }
            )
        )
        # A transcription transport can drop while the person is simply silent.
        # That ends one exchange but not the conversation, so it is re-entered on
        # the same durable conversation while the browser socket stays open.
        while await self._exchange_once(connection, conversation_id):
            LOGGER.info("Resuming voice session on the same conversation")

    async def _exchange_once(
        self, connection: ServerConnection, conversation_id: str
    ) -> bool:
        """Run one exchange. Report whether the conversation should resume."""
        try:
            async for event in self._session.exchange(
                conversation_id,
                self._audio(connection, conversation_id),
            ):
                if event.kind is VoiceEventKind.AUDIO:
                    assert event.audio is not None
                    if event.audio.payload:
                        await connection.send(event.audio.payload)
                    if event.audio.final:
                        await connection.send(
                            json.dumps(
                                {
                                    "type": "audio.end",
                                    "media_type": event.audio.media_type,
                                }
                            )
                        )
                    continue
                if event.kind is VoiceEventKind.DIAGNOSTIC:
                    assert event.diagnostic is not None
                    await connection.send(
                        json.dumps(
                            {
                                "type": "diagnostic",
                                **event.diagnostic,
                            }
                        )
                    )
                    continue
                message = {"type": "phase", "value": event.kind.value}
                if event.kind is VoiceEventKind.ERROR:
                    message["reason"] = event.reason
                    LOGGER.error("Voice session failed: %s", event.reason)
                    await connection.send(json.dumps(message))
                    if event.reason in RECOVERABLE_TRANSPORT_REASONS:
                        await connection.send(
                            json.dumps({
                                "type": "phase",
                                "value": VoiceEventKind.LISTENING.value,
                            })
                        )
                        return True
                    return False
                await connection.send(json.dumps(message))
        except Exception as error:
            LOGGER.error("Voice transport failed: %s", type(error).__name__)
            await connection.send(
                json.dumps(
                    {
                        "type": "phase",
                        "value": VoiceEventKind.ERROR.value,
                        "reason": "voice_transport_error",
                    }
                )
            )
        return False

    async def _audio(
        self,
        connection: ServerConnection,
        stream_id: str,
    ) -> AsyncIterator[AudioChunk]:
        sequence = 0
        async for payload in connection:
            if not isinstance(payload, bytes) or not payload:
                continue
            if sequence == 0:
                LOGGER.info("First microphone audio frame received")
                await connection.send(
                    json.dumps(
                        {
                            "type": "diagnostic",
                            "code": "microphone.audio_received",
                        }
                    )
                )
            yield AudioChunk(
                stream_id,
                sequence,
                payload,
                "audio/pcm",
                self._sample_rate_hz,
            )
            sequence += 1

    def _serve_asset(
        self,
        connection: ServerConnection,
        request: Request,
    ) -> Response | None:
        parsed = urlsplit(request.path)
        if parsed.path == "/voice":
            return None
        relative = {
            "/": "index.html",
            "/app.css": "app.css",
            "/app.js": "app.js",
            "/pcm-worklet.js": "pcm-worklet.js",
            "/background.mp4": "background.mp4",
        }.get(parsed.path)
        if relative is None:
            return self._response(404, b"Not found", "text/plain; charset=utf-8")
        path = (self._asset_root / relative).resolve()
        if path.parent != self._asset_root or not path.is_file():
            return self._response(404, b"Not found", "text/plain; charset=utf-8")
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if media_type.startswith("text/") or media_type == "application/javascript":
            media_type += "; charset=utf-8"
        return self._response(200, path.read_bytes(), media_type)

    @staticmethod
    def _conversation_id(query: dict[str, list[str]]) -> str:
        proposed = query.get("conversation_id", [""])[0]
        try:
            return str(UUID(proposed))
        except (ValueError, AttributeError):
            return str(uuid4())

    @staticmethod
    def _response(status: int, body: bytes, media_type: str) -> Response:
        reason = "OK" if status == 200 else "Not Found"
        return Response(
            status,
            reason,
            Headers(
                [
                    ("Content-Type", media_type),
                    ("Content-Length", str(len(body))),
                    ("Cache-Control", "no-store"),
                    ("X-Content-Type-Options", "nosniff"),
                ]
            ),
            body,
        )
