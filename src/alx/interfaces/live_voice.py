"""Provider-neutral voice transport into the sole Conversation Gateway."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
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


class VoiceEventKind(str, Enum):
    HEARING = "hearing"
    THINKING = "thinking"
    SPEAKING = "speaking"
    LISTENING = "listening"
    AUDIO = "audio"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class VoiceEvent:
    kind: VoiceEventKind
    audio: AudioChunk | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.kind is VoiceEventKind.AUDIO and self.audio is None:
            raise ValueError("audio events require an audio chunk")
        if self.kind is not VoiceEventKind.AUDIO and self.audio is not None:
            raise ValueError("only audio events may carry an audio chunk")
        if self.kind is VoiceEventKind.ERROR and not self.reason:
            raise ValueError("error events require a reason")
        if self.kind is not VoiceEventKind.ERROR and self.reason is not None:
            raise ValueError("only error events may carry a reason")


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

    async def exchange(
        self,
        conversation_id: str,
        audio: AsyncIterable[AudioChunk],
    ) -> AsyncIterator[VoiceEvent]:
        if not conversation_id.strip():
            raise ValueError("conversation_id must not be blank")
        async for transcription in self._transcriber.transcribe(audio):
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
            try:
                outcome = self._gateway.receive_conversation_turn(
                    turn,
                    self._step_budget,
                    now + timedelta(days=self._retention_days),
                )
            except Exception:
                yield VoiceEvent(VoiceEventKind.ERROR, reason="conversation_gateway_error")
                return

            if outcome.response is None:
                yield VoiceEvent(
                    VoiceEventKind.ERROR,
                    reason=outcome.reason or "authoritative_response_missing",
                )
                return

            yield VoiceEvent(VoiceEventKind.SPEAKING)
            try:
                async for chunk in self._synthesizer.synthesize(outcome.response):
                    yield VoiceEvent(VoiceEventKind.AUDIO, audio=chunk)
            except Exception:
                yield VoiceEvent(VoiceEventKind.ERROR, reason="speech_synthesis_error")
                return
            yield VoiceEvent(VoiceEventKind.LISTENING)
