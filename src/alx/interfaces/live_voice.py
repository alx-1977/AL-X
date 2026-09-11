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


# A session may remain open indefinitely. This is only a transient guard for
# observations the durable source could not mark delivered, so it must not turn
# each distinct observation into permanent session memory.
MAX_CARRIED_BACKGROUND_IDS = 256


def _remember_carried_background(
    event_id: str,
    event_ids: set[str],
    event_order: deque[str],
) -> None:
    """Remember one unreconciled observation without unbounded session growth."""
    if event_id in event_ids:
        return
    if len(event_order) == MAX_CARRIED_BACKGROUND_IDS:
        event_ids.remove(event_order.popleft())
    event_ids.add(event_id)
    event_order.append(event_id)


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


class VoiceDiagnosticBuffer:
    """Thread-safe, content-free development telemetry grouped by conversation."""

    def __init__(self, max_events_per_conversation: int = 256) -> None:
        if max_events_per_conversation <= 0:
            raise ValueError("max_events_per_conversation must be positive")
        self._events: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=max_events_per_conversation)
        )
        self._lock = Lock()

    def publish(self, conversation_id: str, values: Mapping[str, Any]) -> None:
        if not conversation_id.strip():
            return
        event = dict(values)
        with self._lock:
            events = self._events[conversation_id]
            task_id = event.get("task_id")
            if (
                event.get("code") == "task.status"
                and isinstance(task_id, str)
                and task_id.strip()
            ):
                events = deque(
                    (
                        current
                        for current in events
                        if current.get("code") != "task.status"
                        or current.get("task_id") != task_id
                    ),
                    maxlen=events.maxlen,
                )
                self._events[conversation_id] = events
            events.append(event)

    def drain(self, conversation_id: str) -> tuple[dict[str, Any], ...]:
        with self._lock:
            events = tuple(self._events.pop(conversation_id, ()))
        return events


class VoiceActivityStatus:
    """Thread-safe current runtime activity for the existing voice event path.

    Coding sessions run on Core's worker thread. Subscribers are notified
    directly when that thread enters a lifecycle boundary, so the terminal can
    update without waiting for the Core turn to return or making another model
    call.
    """

    def __init__(self) -> None:
        self._value = "reasoning"
        self._listeners: set[Callable[[str], None]] = set()
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

    def subscribe(self, listener: Callable[[str], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.add(listener)

        def unsubscribe() -> None:
            with self._lock:
                self._listeners.discard(listener)

        return unsubscribe


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
        self._event_source = event_source
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
        # Background work that arrived while a person was already waiting, kept
        # in arrival order until the person path is idle.
        #
        # One queue carries every source, and it is strictly first-in-first-out.
        # Mail observations re-emit each poll cycle until a turn records their
        # delivery, so an observation AL/X answers silently is offered again on
        # the next cycle. A background turn takes longer than the poll interval,
        # so the queue gains events faster than it drains, and typed input added
        # behind that backlog is never reached: on 2026-09-08 five consecutive
        # background turns ran and two typed messages were never processed at
        # all.
        #
        # Ordering rather than exclusion. Nothing here weighs how interesting
        # an item is: the only question asked is which source it came from, so
        # a person waiting is served before queued background work. Background
        # Distinct background work is not dropped, rate-limited or deferred by
        # a timer. Equivalent re-emissions are coalesced below, and D-024
        # continues exactly as before once nothing is waiting.
        deferred_background: deque[tuple[str, Any]] = deque()
        # An undelivered observation is re-emitted with the same durable
        # identity. Keep its first queued occurrence and coalesce later copies;
        # distinct observations retain their arrival order and are never
        # truncated. The identifier leaves this set when its entry leaves the
        # deque, allowing a still-undelivered observation to be offered again.
        deferred_background_ids: set[str] = set()
        # Unreconciled observations already put in front of Core in this
        # session. The durable record remains authoritative when delivery was
        # recorded; this bounded guard prevents a vanished report, which has no
        # presentation to record, from re-entering Core on every poll cycle.
        carried_background_ids: set[str] = set()
        carried_background_order: deque[str] = deque()

        def defer_background(entry: tuple[str, Any]) -> None:
            event_id = entry[1].event_id
            if event_id in carried_background_ids:
                # Already carried. Re-offering it would buy another reasoning
                # call to reach the same conclusion about the same fact.
                return
            if event_id in deferred_background_ids:
                return
            deferred_background.append(entry)
            deferred_background_ids.add(event_id)

        def take_background() -> tuple[str, Any]:
            entry = deferred_background.popleft()
            deferred_background_ids.remove(entry[1].event_id)
            return entry

        # Set when a background turn stopped without reasoning because the
        # conversation's execution budget was exhausted. While it holds, more
        # background work is deferred rather than run: the next one would take
        # the same millisecond to reach the same checkpoint, and on 2026-09-08
        # that produced 213 of them in one second, which spent the recovery
        # allowance Friedl was about to need. Cleared by the next person turn,
        # which is the only thing that can change the answer.
        background_stopped_on_budget = [False]

        async def next_item() -> tuple[str, Any]:
            """The next thing to work on, person input before background.

            Drains what has already arrived without blocking, so anything
            queued behind a backlog of background events is still found. Only
            when nothing is waiting at all does this block, which leaves the
            idle path identical to a plain queue read.
            """
            while True:
                while True:
                    try:
                        entry = incoming.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if entry[0] == "background":
                        defer_background(entry)
                    else:
                        # A person turn, an error or her own words. Anything
                        # background found on the way keeps its place in
                        # `deferred_background` and runs once this is done.
                        return entry
                if deferred_background and not background_stopped_on_budget[0]:
                    return take_background()
                # Nothing runnable pending: wait, as the plain queue read did.
                entry = await incoming.get()
                if entry[0] != "background":
                    return entry
                defer_background(entry)

        try:
            while True:
                kind, item = await next_item()
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
                    # The console mirrors this exactly as it mirrors an
                    # answer to Friedl. Going straight to synthesis left her
                    # unprompted speech audible with no transcript line: the
                    # words were spoken, and the terminal showed nothing.
                    yield VoiceEvent(VoiceEventKind.TEXT, text=item)
                    # Then the existing synthesis, unaltered. The Core already
                    # decided both that this was worth saying and how to say
                    # it; nothing here rewords or withholds it.
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

                async def run_turn() -> Any:
                    """Keep one Core lock while forwarding worker activity."""
                    async with self._core_turn_lock:
                        self._turn_origin_sink(kind != "background")
                        try:
                            if kind == "background":
                                return await run_core_worker(
                                    self._gateway.receive_background_event,
                                    conversation_id,
                                    item,
                                    self._step_budget,
                                    now + timedelta(days=self._retention_days),
                                )
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
                            self._turn_origin_sink(False)

                updates: asyncio.Queue[str] = asyncio.Queue()
                loop = asyncio.get_running_loop()
                unsubscribe = self._activity.subscribe(
                    lambda value: loop.call_soon_threadsafe(updates.put_nowait, value)
                )
                self._activity.set("reasoning")
                core_task = asyncio.create_task(run_turn())
                try:
                    while not core_task.done():
                        update_task = asyncio.create_task(updates.get())
                        done, _ = await asyncio.wait(
                            (core_task, update_task),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if update_task in done:
                            yield VoiceEvent(
                                VoiceEventKind.ACTIVITY,
                                activity=update_task.result(),
                            )
                        else:
                            update_task.cancel()
                            await asyncio.gather(update_task, return_exceptions=True)
                    outcome = await core_task
                    while not updates.empty():
                        yield VoiceEvent(
                            VoiceEventKind.ACTIVITY,
                            activity=updates.get_nowait(),
                        )
                except Exception:
                    yield VoiceEvent(
                        VoiceEventKind.ERROR, reason="conversation_gateway_error"
                    )
                    return
                finally:
                    unsubscribe()

                # A person checkpoint grants recovery but has not made budget
                # headroom for background work. Keep it suppressed until a
                # person turn reaches a different outcome.
                if outcome.reason == "budget_exceeded":
                    if not background_stopped_on_budget[0]:
                        LOGGER.info(
                            "Deferring background work: the conversation's "
                            "execution budget is exhausted"
                        )
                    background_stopped_on_budget[0] = True
                elif kind != "background":
                    background_stopped_on_budget[0] = False

                delivered = True
                async for response_event in self._response_events(
                    conversation_id, outcome
                ):
                    if response_event.kind is VoiceEventKind.ERROR:
                        delivered = False
                    yield response_event
                if kind == "background" and delivered:
                    assert self._event_source is not None
                    recorded = await run_core_worker(
                        self._event_source.record_delivery, item.event_id
                    )
                    if not recorded:
                        # The observation was reconciled away while she was
                        # answering it -- acknowledged in the same turn, or
                        # cleared by a later scan. She has already spoken, so
                        # there is nothing to repair and nothing to say; the
                        # session continues.
                        #
                        # False is not "undelivered". It means no presentation
                        # was recorded, which is also what a vanished report
                        # always returns: it announces no mail, so it presents
                        # nothing. Treating that as a failed delivery offered
                        # the same disappearance again on the next cycle, and
                        # each re-offer spent a full reasoning call to conclude
                        # there was nothing to say. Whether the delivery was
                        # carried is a separate question, and the durable flag
                        # already answers it.
                        LOGGER.info(
                            "Mail delivery already reconciled: %s",
                            item.event_id,
                        )
                    if not recorded:
                        _remember_carried_background(
                            item.event_id,
                            carried_background_ids,
                            carried_background_order,
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
