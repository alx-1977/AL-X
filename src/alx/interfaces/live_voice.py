"""Provider-neutral voice transport into the sole Conversation Gateway."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
import logging
import threading
from threading import Lock
from typing import Any
from uuid import uuid4

from alx.contracts import (
    AudioChunk,
    run_core_worker,
    ConversationOrigin,
    ConversationTurn,
    SpeechSynthesizer,
    SpeechTranscriber,
    TranscriptionState,
)
from alx.contracts.coding import CODING_STALL_SECONDS, CodingTelemetry
from alx.contracts.trace import TraceStatus, TraceSubsystem, emit_trace
from alx.interfaces.diagnostic_feed import VoiceDiagnosticFeed
from alx.conversation import ConversationGateway


LOGGER = logging.getLogger(__name__)


class VoiceEventKind(str, Enum):
    HEARING = "hearing"
    THINKING = "thinking"
    SPEAKING = "speaking"
    LISTENING = "listening"
    AUDIO = "audio"
    DIAGNOSTIC = "diagnostic"
    # Runtime telemetry for the diagnostic terminal. Unlike a phase, this is
    # an explicit lifecycle observation and never authors AL/X's wording.
    ACTIVITY = "activity"
    # AL/X's final wording, for the console. Rendered, never re-derived.
    TEXT = "text"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class VoiceEvent:
    kind: VoiceEventKind
    audio: AudioChunk | None = None
    reason: str | None = None
    diagnostic: Mapping[str, Any] | None = None
    activity: str | None = None
    text: str | None = None
    input_origin: str | None = None

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
        if self.kind is VoiceEventKind.ACTIVITY and not self.activity:
            raise ValueError("activity events require an activity value")
        if self.kind is not VoiceEventKind.ACTIVITY and self.activity is not None:
            raise ValueError("only activity events may carry an activity value")
        if self.kind is VoiceEventKind.ERROR and not self.reason:
            raise ValueError("error events require a reason")
        if self.kind is not VoiceEventKind.ERROR and self.reason is not None:
            raise ValueError("only error events may carry a reason")
        if self.kind is not VoiceEventKind.THINKING and self.input_origin is not None:
            raise ValueError("only thinking events may carry an input origin")


class VoiceActivityStatus:
    """Thread-safe current runtime activity for the existing voice event path.

    Coding sessions run on Core's worker thread. Subscribers are notified
    directly when that thread enters a lifecycle boundary, so the terminal can
    update without waiting for the Core turn to return or making another model
    call.
    """

    def __init__(self) -> None:
        self._value = "reasoning"
        self._listeners: set[Callable[[str | CodingTelemetry], None]] = set()
        self._coding: CodingTelemetry | None = None
        self._next_coding_owner_alive: Callable[[], bool] | None = None
        self._coding_owner_alive: Callable[[], bool] | None = None
        self._lock = Lock()

    def set(self, value: str) -> None:
        if value not in {"reasoning", "coding", "reviewing"}:
            raise ValueError("activity must be reasoning, coding, or reviewing")
        with self._lock:
            if self._value == value:
                return
            self._value = value
            listeners = tuple(self._listeners)
        for listener in listeners:
            listener(value)

    def publish_coding(self, telemetry: CodingTelemetry) -> None:
        """Record the coding worker's own lifecycle observation."""
        with self._lock:
            self._coding = telemetry
            # Capture the task that owned this job when the job reports. A
            # later Core turn may install a different pending task, but it
            # cannot make this telemetry's owner alive again.
            self._coding_owner_alive = self._next_coding_owner_alive
            listeners = tuple(self._listeners)
        for listener in listeners:
            listener(telemetry)

    def set_coding_owner_alive(self, owner_alive: Callable[[], bool]) -> None:
        """Attach the existing Core worker task that owns a coding call."""
        with self._lock:
            self._next_coding_owner_alive = owner_alive

    def coding_snapshot(self, now: datetime) -> dict[str, Any] | None:
        """A present-tense diagnostic derived from state, never console text."""
        with self._lock:
            telemetry = self._coding
            owner_alive = self._coding_owner_alive
        return self._snapshot(telemetry, owner_alive, now)

    def coding_snapshot_for(
        self, telemetry: CodingTelemetry, now: datetime
    ) -> dict[str, Any]:
        """Render one queued transition without replacing current state."""
        with self._lock:
            owner_alive = (
                self._coding_owner_alive
                if self._coding is not None and self._coding.job_id == telemetry.job_id
                else None
            )
        return self._snapshot(telemetry, owner_alive, now)

    @staticmethod
    def _snapshot(
        telemetry: CodingTelemetry | None,
        owner_alive: Callable[[], bool] | None,
        now: datetime,
    ) -> dict[str, Any] | None:
        if telemetry is None:
            return None
        age = max(0, int((now - telemetry.last_activity_at).total_seconds()))
        elapsed = max(0, int((now - telemetry.started_at).total_seconds()))
        phase_elapsed = max(0, int((now - telemetry.phase_started_at).total_seconds()))
        owner_running = owner_alive() if owner_alive is not None else False
        unresponsive = (
            not telemetry.terminal and telemetry.in_flight and not owner_running
        )
        stalled = unresponsive or (
            not telemetry.terminal and not telemetry.in_flight and not telemetry.waiting
            and age >= CODING_STALL_SECONDS
        )
        return {
            "code": "coding.status", "job_id": telemetry.job_id,
            "phase": telemetry.phase, "provider": telemetry.provider,
            "model": telemetry.model,
            "elapsed_seconds": elapsed, "phase_elapsed_seconds": phase_elapsed,
            "last_activity_seconds": age, "attempt": telemetry.attempt,
            "correction_cycle": telemetry.correction_cycle,
            "in_flight": telemetry.in_flight, "waiting": telemetry.waiting,
            "terminal": telemetry.terminal, "outcome": telemetry.outcome,
            "owner_alive": owner_running, "unresponsive": unresponsive,
            "stalled": stalled, "transition": telemetry.transition,
            "branch": telemetry.branch,
        }

    def subscribe(
        self, listener: Callable[[str | CodingTelemetry], None]
    ) -> Callable[[], None]:
        with self._lock:
            self._listeners.add(listener)

        def unsubscribe() -> None:
            with self._lock:
                self._listeners.discard(listener)

        return unsubscribe


class _ExchangeFeed:
    """One exchange's live view of the diagnostic feed. Never blocks, never drops.

    Events arrive from whichever thread published them and are handed to this
    exchange's loop, so the exchange yields them in publication order between
    whatever else it is doing: idle, waiting on a Core turn, or speaking.
    """

    def __init__(
        self,
        feed: VoiceDiagnosticFeed | None,
        conversation_id: str,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._waiter: asyncio.Future[dict[str, Any]] | None = None
        self._conversation_id = conversation_id
        self._unsubscribe: Callable[[], None] = lambda: None
        loop_thread = threading.get_ident()
        if feed is None:
            return

        def deliver(owner: str | None, event: dict[str, Any]) -> None:
            scoped = self._scoped(owner, event)
            if threading.get_ident() == loop_thread:
                # Published on this loop, by the synthesizer for instance:
                # queued at once, so it cannot fall behind the audio chunk
                # that follows it. Deferring it to the next loop turn put
                # "stream connected" after the first audio it announced.
                self._queue.put_nowait(scoped)
                return
            try:
                loop.call_soon_threadsafe(self._queue.put_nowait, scoped)
            except RuntimeError:
                # The loop has closed; this exchange is already gone.
                return

        self._unsubscribe, replay = feed.subscribe(deliver)
        for owner, event in replay:
            self._queue.put_nowait(self._scoped(owner, event))

    def _scoped(self, owner: str | None, event: dict[str, Any]) -> dict[str, Any]:
        """Mark work that belongs to another thread, without naming it."""
        if owner is not None and owner != self._conversation_id:
            return {**event, "background": True}
        return event

    def waiter(self) -> asyncio.Future[dict[str, Any]]:
        if self._waiter is None:
            self._waiter = asyncio.ensure_future(self._queue.get())
        return self._waiter

    def take(self) -> list[dict[str, Any]]:
        """Everything that has arrived, oldest first, without waiting."""
        events: list[dict[str, Any]] = []
        if self._waiter is not None and self._waiter.done():
            if not self._waiter.cancelled():
                events.append(self._waiter.result())
            self._waiter = None
        while not self._queue.empty():
            events.append(self._queue.get_nowait())
        return events

    async def close(self) -> None:
        self._unsubscribe()
        if self._waiter is not None:
            self._waiter.cancel()
            await asyncio.gather(self._waiter, return_exceptions=True)


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
        diagnostics: VoiceDiagnosticFeed | None = None,
        core_turn_lock: asyncio.Lock | None = None,
        turn_origin_sink: Callable[[bool], None] | None = None,
        activity: VoiceActivityStatus | None = None,
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
        # Given by the runtime, so person turns and autonomous turns serialize
        # through one authority. Turn serialization is a property of AL/X
        # having one Core, not of voice: a lock owned here would let an
        # autonomous turn run while Friedl was speaking. One is created here
        # only when no runtime supplied one, which is the test path.
        self._core_turn_lock = core_turn_lock or asyncio.Lock()
        # Told, for each turn, whether a person is waiting on it. The runtime
        # uses it to reserve the budget recovery allowance for Friedl; nothing
        # here reads it back, and the Core is never given it. Transport
        # knowledge stays in the transport.
        self._turn_origin_sink = turn_origin_sink or (lambda _person: None)
        self._activity = activity or VoiceActivityStatus()
        # Person inputs received and not yet answered, by conversation. From
        # the moment a final transcript or typed line arrives until its Core
        # turn returns, the voice belongs to that turn. Touched only on the
        # event loop, which is also where a delivery is admitted.
        self._pending_person_turns: dict[str, int] = {}

    def admits_unprompted_speech(self, conversation_id: str) -> bool:
        """Whether an autonomous response may be queued for speech now.

        Not while a person turn on that conversation is waiting or running.
        That turn's Core reads the conversation after this response was
        stored, so speaking it afterwards repeated what her answer had already
        covered, out of order: on 2026-10-05 one question produced two spoken
        replies, the older one last. Refused here, the response is recorded as
        undelivered, the pending turn's Core is shown that fact, and she
        decides what, if anything, still needs saying. One question, one
        answer. Called on the event loop.
        """
        if not self._pending_person_turns.get(conversation_id):
            return True
        self._trace(
            TraceSubsystem.VOICE, TraceStatus.INFO,
            "Background response held for pending turn", conversation_id,
        )
        return False

    def _adjust_pending(self, conversation_id: str, change: int) -> None:
        remaining = self._pending_person_turns.get(conversation_id, 0) + change
        if remaining > 0:
            self._pending_person_turns[conversation_id] = remaining
        else:
            self._pending_person_turns.pop(conversation_id, None)

    def _trace(self, subsystem: TraceSubsystem, status: TraceStatus, label: str,
               conversation_id: str | None = None, **values: Any) -> None:
        emit_trace(
            None if self._diagnostics is None else self._diagnostics.trace,
            subsystem, status, label, conversation_id=conversation_id, **values,
        )

    async def exchange(
        self,
        conversation_id: str,
        audio: AsyncIterable[AudioChunk],
        deliveries: "asyncio.Queue[str] | None" = None,
        typed: "asyncio.Queue[str] | None" = None,
        turn_started: Callable[[], None] | None = None,
        turn_finished: Callable[[], None] | None = None,
    ) -> AsyncIterator[VoiceEvent]:
        if not conversation_id.strip():
            raise ValueError("conversation_id must not be blank")
        incoming: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        feed = _ExchangeFeed(self._diagnostics, conversation_id, loop)
        # Person inputs this exchange registered as pending and has not yet
        # settled, so leaving early releases exactly what it holds.
        registered = 0

        def person_input_arrived() -> None:
            nonlocal registered
            registered += 1
            self._adjust_pending(conversation_id, 1)

        def person_input_settled() -> None:
            nonlocal registered
            if registered:
                registered -= 1
                self._adjust_pending(conversation_id, -1)

        async def receive_transcriptions() -> None:
            try:
                async for item in self._transcriber.transcribe(audio):
                    if item.state is not TranscriptionState.PARTIAL:
                        person_input_arrived()
                    await incoming.put(("transcription", item))
                await incoming.put(("transcription_end", None))
            except Exception:
                await incoming.put(("error", "speech_transcription_error"))

        async def receive_typed_lines() -> None:
            """What Friedl typed, on its way to the one person-turn path."""
            assert typed is not None
            while True:
                line = await typed.get()
                person_input_arrived()
                await incoming.put(("typed", line))

        tasks = [asyncio.create_task(receive_transcriptions())]
        if typed is not None:
            tasks.append(asyncio.create_task(receive_typed_lines()))
        input_waiter: asyncio.Future[tuple[str, Any]] | None = None
        # Her own words, arriving from a turn nobody asked for. Queued by the
        # transport when an autonomous turn returns RESPONDED and spoken here
        # through the same synthesis a person turn uses. Read directly rather
        # than relayed, so nothing sits in flight between admission and here.
        delivery_waiter: asyncio.Future[str] | None = None
        try:
            while True:
                if input_waiter is None:
                    input_waiter = asyncio.ensure_future(incoming.get())
                waiters: list[asyncio.Future[Any]] = [input_waiter, feed.waiter()]
                if deliveries is not None:
                    if delivery_waiter is None:
                        delivery_waiter = asyncio.ensure_future(deliveries.get())
                    waiters.append(delivery_waiter)
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                for event in feed.take():
                    yield VoiceEvent(VoiceEventKind.DIAGNOSTIC, diagnostic=event)
                # A delivery is admitted only while no person input is pending,
                # so any delivery waiting here was admitted before every person
                # input now queued. It is spoken first, which keeps what was
                # heard in the order the conversation recorded it.
                spoken: list[str] = []
                if delivery_waiter is not None and delivery_waiter.done():
                    spoken.append(delivery_waiter.result())
                    delivery_waiter = None
                while deliveries is not None and not deliveries.empty():
                    spoken.append(deliveries.get_nowait())
                for response in spoken:
                    # The console mirrors this exactly as it mirrors an
                    # answer to Friedl. Going straight to synthesis left her
                    # unprompted speech audible with no transcript line: the
                    # words were spoken, and the terminal showed nothing.
                    yield VoiceEvent(VoiceEventKind.TEXT, text=response)
                    # Then the existing synthesis, unaltered. The Core already
                    # decided both that this was worth saying and how to say
                    # it; nothing here rewords it.
                    async for speech_event in self._speak(conversation_id, response, feed):
                        yield speech_event
                if not input_waiter.done():
                    continue
                kind, item = input_waiter.result()
                input_waiter = None
                if kind == "error":
                    yield VoiceEvent(VoiceEventKind.ERROR, reason=item)
                    return
                if kind == "transcription_end":
                    return
                if kind == "transcription" and item.state is TranscriptionState.PARTIAL:
                    LOGGER.info("Cartesia event received: %s", item.state.value)
                    yield VoiceEvent(VoiceEventKind.HEARING)
                    continue

                now = self._clock()
                if now.tzinfo is None or now.utcoffset() is None:
                    yield VoiceEvent(VoiceEventKind.ERROR, reason="clock_error")
                    return
                input_origin = {
                    "transcription": "speech_transcript",
                    "typed": "typed",
                }[kind]
                yield VoiceEvent(VoiceEventKind.THINKING, input_origin=input_origin)
                LOGGER.info("Authoritative Core turn started: %s", kind)
                if self._core_turn_lock.locked():
                    # The silence Friedl could not explain: his turn waiting
                    # behind a background turn that already held the Core.
                    self._trace(
                        TraceSubsystem.CORE, TraceStatus.WAITING,
                        "Waiting for background work to finish", conversation_id,
                    )

                async def run_turn() -> Any:
                    """Keep one Core lock while forwarding worker activity."""
                    async with self._core_turn_lock:
                        # Bind a coding job to this turn only once this turn
                        # owns the shared Core lock. A queued later turn
                        # cannot replace the callback before this turn's first
                        # CodingTelemetry publication.
                        assert core_task is not None
                        self._activity.set_coding_owner_alive(
                            lambda task=core_task: not task.done()
                        )
                        self._turn_origin_sink(True)
                        if turn_started is not None:
                            turn_started()
                        try:
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
                            return await run_core_worker(
                                self._gateway.receive_conversation_turn,
                                turn,
                                self._step_budget,
                                now + timedelta(days=self._retention_days),
                            )
                        finally:
                            try:
                                self._turn_origin_sink(False)
                            finally:
                                if turn_finished is not None:
                                    turn_finished()

                updates: asyncio.Queue[str | CodingTelemetry] = asyncio.Queue()
                unsubscribe = self._activity.subscribe(
                    lambda value: loop.call_soon_threadsafe(updates.put_nowait, value)
                )
                self._activity.set("reasoning")
                core_task: asyncio.Task[Any] | None = asyncio.create_task(run_turn())
                try:
                    try:
                        while not core_task.done():
                            update_task = asyncio.create_task(updates.get())
                            done, _ = await asyncio.wait(
                                (core_task, update_task, feed.waiter()),
                                timeout=1.0, return_when=asyncio.FIRST_COMPLETED,
                            )
                            # Live, while the turn is still running: this is
                            # what makes a long turn legible as it happens.
                            for event in feed.take():
                                yield VoiceEvent(VoiceEventKind.DIAGNOSTIC, diagnostic=event)
                            if update_task in done:
                                update = update_task.result()
                                if isinstance(update, CodingTelemetry):
                                    yield VoiceEvent(
                                        VoiceEventKind.DIAGNOSTIC,
                                        diagnostic=self._activity.coding_snapshot_for(
                                            update, self._clock()
                                        ),
                                    )
                                else:
                                    yield VoiceEvent(VoiceEventKind.ACTIVITY, activity=update)
                                continue
                            update_task.cancel()
                            await asyncio.gather(update_task, return_exceptions=True)
                            if not done:
                                snapshot = self._activity.coding_snapshot(self._clock())
                                if snapshot is not None and not snapshot["terminal"]:
                                    yield VoiceEvent(VoiceEventKind.DIAGNOSTIC, diagnostic=snapshot)
                        snapshot = self._activity.coding_snapshot(self._clock())
                        if snapshot is not None and not snapshot["terminal"]:
                            yield VoiceEvent(VoiceEventKind.DIAGNOSTIC, diagnostic=snapshot)
                        outcome = await core_task
                    finally:
                        # The Core has read and answered; a delivery admitted
                        # from here on is newer than this turn's answer.
                        person_input_settled()
                    while not updates.empty():
                        update = updates.get_nowait()
                        if isinstance(update, CodingTelemetry):
                            yield VoiceEvent(
                                VoiceEventKind.DIAGNOSTIC,
                                diagnostic=self._activity.coding_snapshot_for(
                                    update, self._clock()
                                ),
                            )
                        else:
                            yield VoiceEvent(VoiceEventKind.ACTIVITY, activity=update)
                except Exception:
                    yield VoiceEvent(
                        VoiceEventKind.ERROR, reason="conversation_gateway_error"
                    )
                    return
                finally:
                    unsubscribe()

                async for response_event in self._response_events(
                    conversation_id, outcome, feed
                ):
                    yield response_event
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if (delivery_waiter is not None and delivery_waiter.done()
                    and not delivery_waiter.cancelled() and deliveries is not None):
                # Taken from the queue but never spoken. Put back, so the
                # exchange that resumes this conversation speaks it rather
                # than losing words the transport already reported delivered.
                deliveries.put_nowait(delivery_waiter.result())
                delivery_waiter = None
            for waiter in (input_waiter, delivery_waiter):
                if waiter is not None:
                    waiter.cancel()
                    await asyncio.gather(waiter, return_exceptions=True)
            await feed.close()
            if registered:
                self._adjust_pending(conversation_id, -registered)

    async def _response_events(
        self, conversation_id, outcome, feed: _ExchangeFeed | None = None,
    ):
        for event in () if feed is None else feed.take():
            yield VoiceEvent(VoiceEventKind.DIAGNOSTIC, diagnostic=event)
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
            if outcome.reason == "autonomous_reasoning_disabled":
                yield VoiceEvent(
                    VoiceEventKind.DIAGNOSTIC,
                    diagnostic={"code": "autonomous.reasoning_disabled"},
                )
            yield VoiceEvent(VoiceEventKind.LISTENING)
            return
        if outcome.state.value == "checkpointed":
            # A checkpoint has preserved the durable workflow and deliberately
            # stopped this turn. The transport reports that mechanical fact,
            # but cannot compose an answer in AL/X's voice or turn it into a
            # fatal session error.
            yield VoiceEvent(
                VoiceEventKind.DIAGNOSTIC,
                diagnostic={
                    "code": "core.checkpointed",
                    "reason": outcome.reason or "checkpointed",
                },
            )
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
        async for speech_event in self._speak(conversation_id, outcome.response, feed):
            yield speech_event

    async def _speak(
        self, conversation_id: str, response: str, feed: _ExchangeFeed | None = None,
    ):
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
        chunks = aiter(self._synthesizer.synthesize(response, conversation_id))
        next_chunk: asyncio.Future[Any] | None = None
        try:
            while True:
                # Synthesis stages stream while the provider is still
                # connecting, not only once the first chunk arrives, and any
                # already published are yielded ahead of the audio they
                # describe.
                next_chunk = asyncio.ensure_future(anext(chunks))
                while feed is not None and not next_chunk.done():
                    await asyncio.wait(
                        (next_chunk, feed.waiter()),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for event in feed.take():
                        yield VoiceEvent(VoiceEventKind.DIAGNOSTIC, diagnostic=event)
                try:
                    chunk = await next_chunk
                except StopAsyncIteration:
                    break
                for event in () if feed is None else feed.take():
                    yield VoiceEvent(VoiceEventKind.DIAGNOSTIC, diagnostic=event)
                yield VoiceEvent(VoiceEventKind.AUDIO, audio=chunk)
        except Exception:
            yield VoiceEvent(VoiceEventKind.ERROR, reason="speech_synthesis_error")
            return
        finally:
            if next_chunk is not None and not next_chunk.done():
                # Closed by the consumer mid-read: stop the read before the
                # generator itself, which cannot be closed while running.
                next_chunk.cancel()
                await asyncio.gather(next_chunk, return_exceptions=True)
            close = getattr(chunks, "aclose", None)
            if close is not None:
                await close()
        LOGGER.info("Speech synthesis completed")
        yield VoiceEvent(VoiceEventKind.LISTENING)
