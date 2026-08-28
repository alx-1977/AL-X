"""Local browser microphone and playback transport for a VoiceSession."""

from __future__ import annotations

import json
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
        await connection.send(
            json.dumps(
                {
                    "type": "session.ready",
                    "conversation_id": conversation_id,
                    "sample_rate_hz": self._sample_rate_hz,
                }
            )
        )
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
                message = {"type": "phase", "value": event.kind.value}
                if event.kind is VoiceEventKind.ERROR:
                    message["reason"] = event.reason
                await connection.send(json.dumps(message))
        except Exception:
            await connection.send(
                json.dumps(
                    {
                        "type": "phase",
                        "value": VoiceEventKind.ERROR.value,
                        "reason": "voice_transport_error",
                    }
                )
            )

    async def _audio(
        self,
        connection: ServerConnection,
        stream_id: str,
    ) -> AsyncIterator[AudioChunk]:
        sequence = 0
        async for payload in connection:
            if not isinstance(payload, bytes) or not payload:
                continue
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
