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
# A step-budget stop is a durable checkpoint, not a failed session. The Core
# has persisted the goal and can continue it on the next turn, so the transport
# must keep listening instead of hanging up on work that is still in progress.
# A reasoning provider that returns nothing usable has failed one turn, not the
# conversation: the model answered blank after 154 seconds and the session
# ended silently, so Friedl was left waiting on a turn that was already dead.
# The Core rejected that decision and changed nothing, so there is no partial
# work to protect by hanging up. This is a provider fault like a dropped
# socket, not an invalid decision AL/X acted on.
# A dispatch blocked by goal eligibility is the Core stopping on purpose after
# one decision rather than buying more reasoning from the same state. Nothing
# acted and nothing was recorded, so the transport keeps listening for the
# turn that resolves it.
# A memory that could not be persisted is a storage fault, not a decision AL/X
# acted on. Prevention belongs upstream and is where the real fix lives: an
# identifier the Core reused is now harmless and the protocol states the rule.
# This is only what remains when storage itself fails, a full disk or a locked
# database. Losing the conversation is then the worse of the two failures:
# nothing external happened, the goal is unchanged, and a dispatch that was
# checkpointed but never sent is closed as an unknown outcome on the next turn
# rather than repeated. The diagnostics panel still names the fault.
RECOVERABLE_TRANSPORT_REASONS = frozenset(
    {
        "speech_transcription_error",
        "budget_exhausted",
        "budget_exceeded",
        "reasoner_error",
        "active_goal_required",
        "memory_persistence_error",
    }
)

# Where recovery happens decides whether AL/X can still hear.
#
# These three are raised mid-exchange while the transcriber and the microphone
# iterator are both alive, and `exchange()` already yields LISTENING and keeps
# running afterwards. Returning here would abandon that live audio iterator and
# start a second consumer of the same browser socket, so the next thing Friedl
# said reached nobody: the session survived but was deaf, which is worse than
# ending. They are recovered inside the existing exchange.
#
# `speech_transcription_error` is different: it ends `exchange()` itself, so
# there is no exchange left to continue and re-entry is the only way back.
MID_EXCHANGE_RECOVERABLE_REASONS = frozenset(
    {
        "budget_exhausted", "budget_exceeded", "reasoner_error",
        "active_goal_required", "memory_persistence_error",
    }
)


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
        # Set when a mid-exchange recovery happens, cleared by the next audio
        # frame. It proves the microphone iterator survived the recovery.
        self._await_audio_confirmation = False
        # How many voice connections are open. A count rather than a flag, so
        # two browsers closing one tab does not read as silence.
        self._live_connections = 0

    def has_live_transport(self) -> bool:
        """Whether a voice connection could carry speech right now.

        Asked only after the Core has already decided to speak, so it never
        influences whether cognition happens. Absence of a listener must not
        stop her thinking; it only means what she said had nowhere to go.
        """
        return self._live_connections > 0

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
        self._live_connections += 1
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
        try:
            while await self._exchange_once(connection, conversation_id):
                LOGGER.info("Resuming voice session on the same conversation")

        finally:
            self._live_connections -= 1

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
                    # Law 1: the transport reports a structural phase and a
                    # reason code. It does not compose wording on AL/X's
                    # behalf. A failed turn is visible as an error phase and in
                    # the diagnostics panel; anything said to Friedl about it
                    # must come from the authoritative reasoning path.
                    await connection.send(json.dumps(message))
                    if event.reason in MID_EXCHANGE_RECOVERABLE_REASONS:
                        # The exchange is still running and still owns the
                        # microphone iterator. It yields its own LISTENING
                        # next, so this neither sends one nor re-enters.
                        LOGGER.info(
                            "Recovering inside the running exchange: %s",
                            event.reason,
                        )
                        self._await_audio_confirmation = True
                        await connection.send(
                            json.dumps(
                                {
                                    "type": "diagnostic",
                                    "code": "voice.recovered_in_exchange",
                                    "reason": event.reason,
                                }
                            )
                        )
                        continue
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
            elif self._await_audio_confirmation:
                # Proof the same iterator is still carrying audio after a
                # mid-exchange recovery. Silence here is how a deaf session
                # looked last time: the turn recovered, the browser kept
                # sending, and nothing arrived. A technical code, not wording.
                self._await_audio_confirmation = False
                LOGGER.info("Microphone audio resumed after recovery")
                await connection.send(
                    json.dumps(
                        {
                            "type": "diagnostic",
                            "code": "microphone.audio_resumed",
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
