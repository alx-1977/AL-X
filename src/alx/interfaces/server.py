"""Local browser microphone and playback transport for a VoiceSession."""

from __future__ import annotations

import asyncio
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

from typing import Any

from alx.contracts import AudioChunk, ResponseDelivery
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
# The one non-audio frame the socket accepts. A transport shape, like
# "audio.end" in the other direction; it names a frame format and never a
# meaning, and nothing branches on what the frame carries.
TYPED_FRAME = "person.text"

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
        # Live delivery queues per conversation. An autonomous response is
        # handed to a real listener or it is undelivered; a count of open
        # sockets cannot tell the difference.
        self._delivery_queues: dict[str, list[Any]] = {}
        # Typed lines waiting to become person turns. Separate from delivery
        # because one carries input and the other carries output; conflating
        # them is how a console starts inferring where text should go.
        self._typed_queues: dict[str, list[Any]] = {}

    def deliver(self, conversation_id: str, response: str) -> ResponseDelivery:
        """Speak an autonomous response through the one existing synthesis path.

        Delivery means a live connection accepted the response for synthesis,
        never merely that a socket exists: a response handed to nobody is
        undelivered even with a browser attached, and reporting otherwise would
        discard her words while recording success.

        There is deliberately no second speech implementation here. The audio
        is produced by the same synthesizer the person-turn path uses, and the
        wording is the Core's own, unaltered.
        """
        deliveries = [
            queue
            for queue in self._delivery_queues.get(conversation_id, ())
        ]
        if not deliveries:
            return ResponseDelivery.UNDELIVERABLE
        accepted = False
        for queue in deliveries:
            try:
                queue.put_nowait(response)
                accepted = True
            except Exception:
                # A queue that cannot take it is a listener that is going away.
                continue
        return (
            ResponseDelivery.DELIVERED if accepted
            else ResponseDelivery.UNDELIVERABLE
        )

    # A typed line is at most this long. Not a judgement about what is worth
    # saying: a bound so one frame cannot exhaust memory or the input ceiling.
    MAX_TYPED_CHARACTERS = 8_000

    def _queue_typed_turn(self, conversation_id: str, payload: str) -> None:
        """Hand a typed line to the session, or drop a malformed frame.

        Dropping rather than raising: a browser sending nonsense must not end a
        conversation, and there is nothing here to tell Friedl that would not be
        the transport speaking in AL/X's voice.
        """
        try:
            frame = json.loads(payload)
        except (ValueError, TypeError):
            LOGGER.info("Ignoring an unparseable console frame")
            return
        # A wire-protocol discriminator, not language routing: this reads the
        # frame's declared shape, never what Friedl wrote. The line itself is
        # carried to the Core untouched and uninspected.
        if not isinstance(frame, dict) or frame.get("type") != TYPED_FRAME:
            LOGGER.info("Ignoring an unsupported console frame")
            return
        content = frame.get("content")
        if not isinstance(content, str) or not content.strip():
            return
        if len(content) > self.MAX_TYPED_CHARACTERS:
            LOGGER.info("Ignoring an oversized console frame")
            return
        queues = getattr(self, "_typed_queues", {}).get(conversation_id) or []
        for queue in queues:
            try:
                queue.put_nowait(content)
            except Exception:
                continue

    def _delivery_queue(self, conversation_id: str):
        """The queue this exchange should drain, if a listener registered one."""
        queues = getattr(self, "_delivery_queues", {}).get(conversation_id) or []
        return queues[-1] if queues else None

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
        # Registered while this connection can actually carry speech, so an
        # autonomous response is offered to a live listener rather than to a
        # socket count that cannot say whether anyone took it.
        deliveries: asyncio.Queue[str] = asyncio.Queue()
        self._delivery_queues.setdefault(conversation_id, []).append(deliveries)
        typed: asyncio.Queue[str] = asyncio.Queue()
        self._typed_queues.setdefault(conversation_id, []).append(typed)
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
            queues = self._delivery_queues.get(conversation_id, [])
            if deliveries in queues:
                queues.remove(deliveries)
            if not queues:
                self._delivery_queues.pop(conversation_id, None)
            waiting = self._typed_queues.get(conversation_id, [])
            if typed in waiting:
                waiting.remove(typed)
            if not waiting:
                self._typed_queues.pop(conversation_id, None)

    async def _exchange_once(
        self, connection: ServerConnection, conversation_id: str
    ) -> bool:
        """Run one exchange. Report whether the conversation should resume."""
        try:
            async for event in self._session.exchange(
                conversation_id,
                self._audio(connection, conversation_id),
                self._delivery_queue(conversation_id),
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
                if event.kind is VoiceEventKind.TEXT:
                    await connection.send(
                        json.dumps(
                            {
                                "type": "alx.text",
                                "stream": "ALX",
                                "content": event.text,
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
            if isinstance(payload, str):
                # The only non-audio frame the socket accepts. It carries what
                # Friedl typed and nothing else: no command, no destination, no
                # grammar. Where it goes is decided here, not by what it says.
                self._queue_typed_turn(stream_id, payload)
                continue
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
