"""Live, content-free runtime telemetry for connected consoles."""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from threading import Lock, RLock
from typing import Any

from alx.contracts.trace import TraceEvent


LOGGER = logging.getLogger(__name__)


class VoiceDiagnosticFeed:
    """Live, content-free runtime telemetry for every connected console.

    Each event is stamped with the time and sequence of its publication, here,
    as it happens. The previous buffer held events until a person turn
    finished, so a two-minute turn showed nothing and then a burst of lines
    that all carried the browser's arrival time.

    Events are runtime-wide: AL/X has one Core, and work for one thread (a mail
    occasion, a plan step) is still what the runtime is doing. A listener is
    told which conversation an event belongs to so it can mark background work;
    the conversation identifier itself never enters the event, because a
    mail-thread identifier names its sender's domain.

    Nothing is buffered for an absent console except current *state* rows,
    such as a running task, which are replayed to the next listener so a
    reconnecting console shows what is outstanding rather than nothing.
    """

    # Codes that describe a current state rather than a moment. Keyed so the
    # newest report replaces the last; settled states are released.
    _STATE_KEYS = {
        "task.status": ("task_id",),
        "plan.attention": ("goal_id", "plan_id", "attention_seq"),
    }
    _SETTLED_TASK_STATES = frozenset({"completed", "failed", "observer_unavailable"})

    def __init__(
        self,
        clock: Callable[[], datetime] | None = None,
        max_state_rows: int = 64,
    ) -> None:
        if max_state_rows <= 0:
            raise ValueError("max_state_rows must be positive")
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max_state_rows = max_state_rows
        self._sequence = 0
        self._states: dict[tuple[Any, ...], tuple[str | None, dict[str, Any]]] = {}
        self._listeners: set[Callable[[str | None, dict[str, Any]], None]] = set()
        self._lock = Lock()
        # Held from stamping through delivery. Listeners only enqueue, so the
        # time any publisher waits on another is the length of a queue append.
        # Re-entrant, so a listener that publishes cannot deadlock itself.
        self._dispatch = RLock()
        # Stamped events not yet delivered, oldest first, and whether a
        # delivery loop is running. Touched only while holding `_dispatch`.
        self._undelivered: deque[tuple[str | None, dict[str, Any]]] = deque()
        self._delivering = False

    def publish(self, conversation_id: str | None, values: Mapping[str, Any]) -> None:
        """Stamp one event now and hand it to every live listener.

        Every listener receives events in sequence order. Publishers on other
        threads wait on the dispatch lock, so the Core worker and a planned
        step cannot overtake each other. A listener that publishes during
        delivery, on the same thread, has its event queued and delivered once
        the current event has reached every listener, rather than ahead of it.
        """
        event = dict(values)
        owner = conversation_id if conversation_id and conversation_id.strip() else None
        with self._dispatch:
            with self._lock:
                self._sequence += 1
                event["seq"] = self._sequence
                event["at"] = self._clock().isoformat(timespec="milliseconds")
                self._remember_state(owner, event)
            self._undelivered.append((owner, event))
            if self._delivering:
                # A nested publish from a listener: the outer delivery loop
                # below reaches it after the event now being delivered.
                return
            self._delivering = True
            try:
                while self._undelivered:
                    next_owner, next_event = self._undelivered.popleft()
                    with self._lock:
                        listeners = tuple(self._listeners)
                    for listener in listeners:
                        try:
                            listener(next_owner, next_event)
                        except Exception:  # noqa: BLE001 - one console must not stop another
                            LOGGER.info("Diagnostic listener failed")
            finally:
                self._delivering = False

    def trace(self, event: TraceEvent) -> None:
        """Publish one operator trace step."""
        self.publish(event.conversation_id, event.values())

    def subscribe(
        self, listener: Callable[[str | None, dict[str, Any]], None]
    ) -> tuple[Callable[[], None], tuple[tuple[str | None, dict[str, Any]], ...]]:
        """Attach a listener; return its detach and the current state rows."""
        with self._lock:
            self._listeners.add(listener)
            replay = tuple(
                sorted(self._states.values(), key=lambda item: item[1]["seq"])
            )

        def unsubscribe() -> None:
            with self._lock:
                self._listeners.discard(listener)

        return unsubscribe, replay

    def _remember_state(self, owner: str | None, event: dict[str, Any]) -> None:
        fields = self._STATE_KEYS.get(event.get("code"))
        if fields is None:
            return
        key = (event["code"], *(event.get(name) for name in fields))
        released = (
            event.get("state") in self._SETTLED_TASK_STATES
            if event["code"] == "task.status"
            else event.get("state") != "blocked"
        )
        if released:
            self._states.pop(key, None)
            return
        self._states[key] = (owner, event)
        while len(self._states) > self._max_state_rows:
            oldest = min(self._states, key=lambda item: self._states[item][1]["seq"])
            self._states.pop(oldest)
