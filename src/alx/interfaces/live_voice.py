"""Provider-neutral voice transport into the sole Conversation Gateway."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import AsyncIterable, AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
import logging
from threading import Lock
from typing import Any
from uuid import uuid4

from alx.contracts import (
    AudioChunk,
    ConversationOrigin,
    ConversationTurn,
    SpeechSynthesizer,
    SpeechTranscriber,
    TranscriptionState,
)
from alx.conversation import ConversationGateway


LOGGER = logging.getLogger(__name__)


class VoiceEventKind(str, Enum):
    HEARING = "hearing"
    THINKING = "thinking"
    SPEAKING = "speaking"
    LISTENING = "listening"
    AUDIO = "audio"
    DIAGNOSTIC = "diagnostic"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class VoiceEvent:
    kind: VoiceEventKind
    audio: AudioChunk | None = None
    reason: str | None = None
    diagnostic: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.kind is VoiceEventKind.AUDIO and self.audio is None:
            raise ValueError("audio events require an audio chunk")
        if self.kind is not VoiceEventKind.AUDIO and self.audio is not None:
            raise ValueError("only audio events may carry an audio chunk")
        if self.kind is VoiceEventKind.DIAGNOSTIC and self.diagnostic is None:
            raise ValueError("diagnostic events require diagnostic values")
        if self.kind is not VoiceEventKind.DIAGNOSTIC and self.diagnostic is not None:
            raise ValueError("only diagnostic events may carry diagnostic values")
        if self.kind is VoiceEventKind.ERROR and not self.reason:
            raise ValueError("error events require a reason")
        if self.kind is not VoiceEventKind.ERROR and self.reason is not None:
            raise ValueError("only error events may carry a reason")


class VoiceDiagnosticBuffer:
    """Thread-safe, content-free development telemetry grouped by conversation."""

    def __init__(self) -> None:
        self._events: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
        self._lock = Lock()

    def publish(self, conversation_id: str, values: Mapping[str, Any]) -> None:
        if not conversation_id.strip():
            return
        with self._lock:
            self._events[conversation_id].append(dict(values))

    def drain(self, conversation_id: str) -> tuple[dict[str, Any], ...]:
        with self._lock:
            events = tuple(self._events.pop(conversation_id, ()))
        return events


class VoiceSession:
    """Move audio and Core outcomes; never infer intent or select capabilities."""

    def __init__(
        self,
        gateway: ConversationGateway,
        transcriber: SpeechTranscriber,
        synthesizer: SpeechSynthesizer,
        person_id: str,
        step_budget: int,
        retention_days: int,
        clock: Callable[[], datetime] | None = None,
        identifier_factory: Callable[[], str] | None = None,
        diagnostics: VoiceDiagnosticBuffer | None = None,
    ) -> None:
        if not person_id.strip():
            raise ValueError("person_id must not be blank")
        if step_budget <= 0:
            raise ValueError("step_budget must be positive")
        if retention_days <= 0:
            raise ValueError("retention_days must be positive")
        self._gateway = gateway
        self._transcriber = transcriber
        self._synthesizer = synthesizer
        self._person_id = person_id
        self._step_budget = step_budget
        self._retention_days = retention_days
        self._clock = clock or (lambda: datetime.now(UTC))
        self._identifier_factory = identifier_factory or (lambda: str(uuid4()))
        self._diagnostics = diagnostics
        self._core_turn_lock = asyncio.Lock()

    async def exchange(
        self,
        conversation_id: str,
        audio: AsyncIterable[AudioChunk],
    ) -> AsyncIterator[VoiceEvent]:
        if not conversation_id.strip():
            raise ValueError("conversation_id must not be blank")
        async for transcription in self._transcriber.transcribe(audio):
            LOGGER.info("Cartesia event received: %s", transcription.state.value)
            if transcription.state is TranscriptionState.PARTIAL:
                yield VoiceEvent(VoiceEventKind.HEARING)
                continue

            now = self._clock()
            if now.tzinfo is None or now.utcoffset() is None:
                yield VoiceEvent(VoiceEventKind.ERROR, reason="clock_error")
                return
            turn = ConversationTurn(
                conversation_id=conversation_id,
                turn_id=self._identifier_factory(),
                origin=ConversationOrigin.SPEECH_TRANSCRIPT,
                content=transcription.content,
                occurred_at=now,
                person_id=self._person_id,
            )
            yield VoiceEvent(VoiceEventKind.THINKING)
            LOGGER.info("Authoritative Core turn started")
            try:
                async with self._core_turn_lock:
                    outcome = await asyncio.to_thread(
                        self._gateway.receive_conversation_turn,
                        turn,
                        self._step_budget,
                        now + timedelta(days=self._retention_days),
                    )
            except Exception:
                if self._diagnostics is not None:
                    for diagnostic in self._diagnostics.drain(conversation_id):
                        yield VoiceEvent(
                            VoiceEventKind.DIAGNOSTIC,
                            diagnostic=diagnostic,
                        )
                yield VoiceEvent(VoiceEventKind.ERROR, reason="conversation_gateway_error")
                return

            if self._diagnostics is not None:
                for diagnostic in self._diagnostics.drain(conversation_id):
                    yield VoiceEvent(
                        VoiceEventKind.DIAGNOSTIC,
                        diagnostic=diagnostic,
                    )

            LOGGER.info(
                "Authoritative Core turn finished: state=%s reason=%s response=%s",
                outcome.state.value,
                outcome.reason,
                outcome.response is not None,
            )

            if outcome.response is None:
                yield VoiceEvent(
                    VoiceEventKind.ERROR,
                    reason=outcome.reason or "authoritative_response_missing",
                )
                yield VoiceEvent(VoiceEventKind.LISTENING)
                continue

            yield VoiceEvent(VoiceEventKind.SPEAKING)
            LOGGER.info("Speech synthesis started")
            try:
                async for chunk in self._synthesizer.synthesize(
                    outcome.response,
                    conversation_id,
                ):
                    if self._diagnostics is not None:
                        for diagnostic in self._diagnostics.drain(conversation_id):
                            yield VoiceEvent(
                                VoiceEventKind.DIAGNOSTIC,
                                diagnostic=diagnostic,
                            )
                    yield VoiceEvent(VoiceEventKind.AUDIO, audio=chunk)
            except Exception:
                yield VoiceEvent(VoiceEventKind.ERROR, reason="speech_synthesis_error")
                return
            LOGGER.info("Speech synthesis completed")
            yield VoiceEvent(VoiceEventKind.LISTENING)
