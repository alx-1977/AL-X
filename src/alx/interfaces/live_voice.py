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
    run_core_worker,
    CognitionOpportunitySource,
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
    # AL/X's final wording, for the console. Rendered, never re-derived.
    TEXT = "text"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class VoiceEvent:
    kind: VoiceEventKind
    audio: AudioChunk | None = None
    reason: str | None = None
    diagnostic: Mapping[str, Any] | None = None
    text: str | None = None

    def __post_init__(self) -> None:
        if self.kind is VoiceEventKind.TEXT and not self.text:
            raise ValueError("text events require AL/X's wording")
        if self.kind is not VoiceEventKind.TEXT and self.text is not None:
            raise ValueError("only text events may carry wording")
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
        synthesizer: SpeechSynthesizer | None,
        person_id: str,
        step_budget: int,
        retention_days: int,
        clock: Callable[[], datetime] | None = None,
        identifier_factory: Callable[[], str] | None = None,
        diagnostics: VoiceDiagnosticBuffer | None = None,
        event_source: CognitionOpportunitySource | None = None,
        core_turn_lock: asyncio.Lock | None = None,
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
        self._event_source = event_source
        # Given by the runtime, so person turns and autonomous turns serialize
        # through one authority. Turn serialization is a property of AL/X
        # having one Core, not of voice: a lock owned here would let an
        # autonomous turn run while Friedl was speaking. One is created here
        # only when no runtime supplied one, which is the test path.
        self._core_turn_lock = core_turn_lock or asyncio.Lock()

    async def exchange(
        self,
        conversation_id: str,
        audio: AsyncIterable[AudioChunk],
        deliveries: "asyncio.Queue[str] | None" = None,
        typed: "asyncio.Queue[str] | None" = None,
    ) -> AsyncIterator[VoiceEvent]:
        if not conversation_id.strip():
            raise ValueError("conversation_id must not be blank")
        incoming: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        async def receive_transcriptions() -> None:
            try:
                async for item in self._transcriber.transcribe(audio):
                    await incoming.put(("transcription", item))
                await incoming.put(("transcription_end", None))
            except Exception:
                await incoming.put(("error", "speech_transcription_error"))

        async def receive_events() -> None:
            assert self._event_source is not None
            try:
                async for item in self._event_source.events():
                    await incoming.put(("background", item))
            except Exception:
                await incoming.put(("error", "background_event_error"))

        async def receive_typed_lines() -> None:
            """What Friedl typed, on its way to the one person-turn path."""
            assert typed is not None
            while True:
                await incoming.put(("typed", await typed.get()))

        async def receive_autonomous_responses() -> None:
            """Her own words, arriving from a turn nobody asked for.

            Queued by the transport when an autonomous turn returns RESPONDED,
            and spoken here through the same synthesis a person turn uses.
            There is no second speech path: this only carries the Core's
            wording to the one that already exists.
            """
            assert deliveries is not None
            while True:
                await incoming.put(("autonomous_response", await deliveries.get()))

        tasks = [asyncio.create_task(receive_transcriptions())]
        if deliveries is not None:
            tasks.append(asyncio.create_task(receive_autonomous_responses()))
        if typed is not None:
            tasks.append(asyncio.create_task(receive_typed_lines()))
        if self._event_source is not None:
            tasks.append(asyncio.create_task(receive_events()))
        try:
            while True:
                kind, item = await incoming.get()
                if kind == "error":
                    yield VoiceEvent(VoiceEventKind.ERROR, reason=item)
                    # A background observation failure leaves speech intact, so the
                    # conversation continues rather than ending. The observation
                    # task has stopped; it is restarted so mail is still watched.
                    if item == "background_event_error" and self._event_source is not None:
                        LOGGER.info("Restarting background observation after failure")
                        tasks.append(asyncio.create_task(receive_events()))
                        yield VoiceEvent(VoiceEventKind.LISTENING)
                        continue
                    return
                if kind == "transcription_end":
                    if self._event_source is None:
                        return
                    continue
                if kind == "autonomous_response":
                    # Straight to the existing synthesis, unaltered. The Core
                    # already decided both that this was worth saying and how
                    # to say it; nothing here rewords or withholds it.
                    async for speech_event in self._speak(conversation_id, item):
                        yield speech_event
                    continue
                if kind == "transcription" and item.state is TranscriptionState.PARTIAL:
                    LOGGER.info("Cartesia event received: %s", item.state.value)
                    yield VoiceEvent(VoiceEventKind.HEARING)
                    continue

                now = self._clock()
                if now.tzinfo is None or now.utcoffset() is None:
                    yield VoiceEvent(VoiceEventKind.ERROR, reason="clock_error")
                    return
                yield VoiceEvent(VoiceEventKind.THINKING)
                LOGGER.info("Authoritative Core turn started: %s", kind)
                try:
                    async with self._core_turn_lock:
                        if kind == "background":
                            outcome = await run_core_worker(
                                self._gateway.receive_background_event,
                                conversation_id,
                                item,
                                self._step_budget,
                                now + timedelta(days=self._retention_days),
                            )
                        else:
                            # Spoken and typed converge here, before the
                            # gateway. They differ only in provenance and in
                            # whether a transcriber was involved; from this
                            # point there is one person-turn path, one Core
                            # call, one conversation and one goal treatment.
                            if kind == "typed":
                                origin = ConversationOrigin.TYPED
                                content = item
                            else:
                                LOGGER.info(
                                    "Cartesia event received: %s", item.state.value
                                )
                                origin = ConversationOrigin.SPEECH_TRANSCRIPT
                                content = item.content
                            turn = ConversationTurn(
                                conversation_id=conversation_id,
                                turn_id=self._identifier_factory(),
                                origin=origin,
                                content=content,
                                occurred_at=now,
                                person_id=self._person_id,
                            )
                            outcome = await run_core_worker(
                                self._gateway.receive_conversation_turn,
                                turn,
                                self._step_budget,
                                now + timedelta(days=self._retention_days),
                            )
                except Exception:
                    yield VoiceEvent(
                        VoiceEventKind.ERROR, reason="conversation_gateway_error"
                    )
                    return

                delivered = True
                async for response_event in self._response_events(
                    conversation_id, outcome
                ):
                    if response_event.kind is VoiceEventKind.ERROR:
                        delivered = False
                    yield response_event
                if kind == "background" and delivered:
                    assert self._event_source is not None
                    await run_core_worker(
                        self._event_source.record_delivery, item.event_id
                    )
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _response_events(self, conversation_id, outcome):
        if self._diagnostics is not None:
            for diagnostic in self._diagnostics.drain(conversation_id):
                yield VoiceEvent(VoiceEventKind.DIAGNOSTIC, diagnostic=diagnostic)
        LOGGER.info(
            "Authoritative Core turn finished: state=%s reason=%s response=%s memory=%s",
            outcome.state.value,
            outcome.reason,
            outcome.response is not None,
            outcome.memory_state or "ok",
        )
        if outcome.state.value == "finished_silently":
            # Silence is an explicit authoritative Core result, not a missing
            # response and not a transport inference. No conversation turn or
            # speech synthesis is created for it.
            yield VoiceEvent(VoiceEventKind.LISTENING)
            return
        if outcome.response is None:
            yield VoiceEvent(
                VoiceEventKind.ERROR,
                reason=outcome.reason or "authoritative_response_missing",
            )
            yield VoiceEvent(VoiceEventKind.LISTENING)
            return
        # The console mirrors what the one response path produced. It is not a
        # second response implementation: the wording is the Core's own, and it
        # is emitted whether or not anything is audible.
        yield VoiceEvent(VoiceEventKind.TEXT, text=outcome.response)
        async for speech_event in self._speak(conversation_id, outcome.response):
            yield speech_event

    async def _speak(self, conversation_id: str, response: str):
        """The one synthesis path. Person turns and autonomous turns share it.

        Extracted rather than duplicated: a second implementation for
        unprompted speech would be a second voice, and the wording reaching it
        is the Core's own either way.
        """
        if self._synthesizer is None:
            # No speech transport. Her wording already reached the console, and
            # the turn is complete; a missing speaker changes nothing about
            # what she decided or what was recorded.
            yield VoiceEvent(VoiceEventKind.LISTENING)
            return
        yield VoiceEvent(VoiceEventKind.SPEAKING)
        LOGGER.info("Speech synthesis started")
        try:
            async for chunk in self._synthesizer.synthesize(
                response, conversation_id
            ):
                if self._diagnostics is not None:
                    for diagnostic in self._diagnostics.drain(conversation_id):
                        yield VoiceEvent(
                            VoiceEventKind.DIAGNOSTIC, diagnostic=diagnostic
                        )
                yield VoiceEvent(VoiceEventKind.AUDIO, audio=chunk)
        except Exception:
            yield VoiceEvent(VoiceEventKind.ERROR, reason="speech_synthesis_error")
            return
        LOGGER.info("Speech synthesis completed")
        yield VoiceEvent(VoiceEventKind.LISTENING)
