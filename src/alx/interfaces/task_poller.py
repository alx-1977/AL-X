"""Watch outstanding external work for the life of the process.

A service handed work does not stop when a browser closes, so what watches it
lives beside the transport rather than inside a session, exactly as the mail
scan and the due-cognition tick do.

This decides nothing. It asks an observer what it can see, records the state,
writes a terminal line, and sleeps. When a result appears it stops watching and
returns to an attached dispatch, or raises one cognition opportunity after a
restart. Both deliver evidence to the existing Core. It never requests a review, retries, spends,
fixes or merges: it imports nothing that could, and a test asserts that.

Terminal lines carry identifiers, states and durations only, which is what
D-012 permits. No finding text passes through here, because none reaches here.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from threading import Condition
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from alx.contracts.task import ExternalTask, TaskState

LOGGER = logging.getLogger(__name__)


def _accepts_consumption(observer: Any) -> bool:
    """Whether this observer takes the consumption fact.

    Only the review observer distinguishes a verdict nobody has read from one
    already delivered; another service's observer has no such notion.
    """
    try:
        return "already_consumed" in inspect.signature(observer.observe).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        return False


class TaskPoller:
    """The external-task tick. It observes and reports, and nothing else."""

    def __init__(
        self,
        store: Any,
        observers: dict[str, Any],
        interval_seconds: float,
        # Given structured values rather than a rendered line: the terminal
        # decides how a running task looks, and this decides nothing.
        announce: Callable[[str, dict], None],
        completed: Callable[[ExternalTask], None],
        fatal_exceptions: tuple[type[Exception], ...] = (),
        clock=None,
        maximum_wait_seconds: float = 900.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._store = store
        self._observers = dict(observers)
        self._interval_seconds = interval_seconds
        # Writes one terminal line. Given as a callback so this never reaches
        # into the transport and cannot be handed anything but a line.
        self._announce = announce
        # Raises the opportunity that wakes the Core. Given the task, never a
        # result: what the result says is read by her from the source.
        self._completed = completed
        self._fatal_exceptions = fatal_exceptions
        self._lock = Condition()
        self._waiting: set[str] = set()
        self._settled: dict[str, ExternalTask] = {}
        if maximum_wait_seconds <= 0:
            raise ValueError("maximum wait must be positive")
        self._maximum_wait = maximum_wait_seconds
        self._now = clock or (lambda: datetime.now(UTC))

    def wait(self, task: ExternalTask) -> str:
        """Join the existing observer path; never return an in-progress result.

        While a dispatch is attached, its terminal evidence returns to that
        dispatch rather than also scheduling an autonomous Core turn. A crash
        leaves the durable outstanding task for the normal background tick.
        """
        with self._lock:
            self._waiting.add(task.task_id)
        try:
            with self._lock:
                self.record(task)
                # Only run()/tick() observes the provider. This dispatch joins
                # that waiter; it does not start another polling loop.
                arrived = self._lock.wait_for(
                    lambda: task.task_id in self._settled,
                    timeout=self._maximum_wait,
                )
                if not arrived:
                    now = self._now()
                    self._finish(replace(task, state=TaskState.FAILED,
                                         last_checked_at=now, completed_at=now))
                settled = self._settled.pop(task.task_id)
                if settled.state is TaskState.COMPLETED:
                    return "completed"
                if not arrived or settled.elapsed_seconds(settled.completed_at) >= self._maximum_wait:
                    return "timed_out"
                return "failed"
        finally:
            with self._lock:
                self._waiting.discard(task.task_id)

    async def run(self) -> None:
        """Tick for the life of the process."""
        while True:
            try:
                await asyncio.to_thread(self.tick)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if isinstance(error, self._fatal_exceptions):
                    raise
                # A failed look is not a failed runtime. The task is still
                # outstanding, and the next tick looks again.
                LOGGER.warning(
                    "External task check failed: %s", type(error).__name__
                )
            await asyncio.sleep(self._interval_seconds)

    def record(self, task: ExternalTask) -> None:
        """Persist and announce a task immediately on first paint."""
        self._store.record(task)
        self._announce(task.conversation_id, self._payload(task, task.requested_at))

    def tick(self) -> None:
        """One look at every outstanding task, serialized with attached waits."""
        with self._lock:
            self._tick()

    def _tick(self) -> None:
        failures: list[Exception] = []
        for task in self._store.outstanding():
            try:
                self._check(task)
            except Exception as error:  # noqa: BLE001 - isolate unrelated tasks
                LOGGER.warning(
                    "External task check failed for %s: %s",
                    task.task_id,
                    type(error).__name__,
                )
                failures.append(error)
        if failures:
            raise failures[0]

    def _check(self, task: ExternalTask) -> None:
        observer = self._observers.get(task.service)
        now = self._now()
        if observer is None and task.task_id not in self._waiting:
            unavailable = replace(task, state=TaskState.OBSERVER_UNAVAILABLE, last_checked_at=now)
            self._store.record(unavailable)
            self._announce(task.conversation_id, self._payload(unavailable, now))
            return
        if task.elapsed_seconds(now) >= self._maximum_wait or observer is None:
            settled = replace(task, state=TaskState.FAILED,
                              last_checked_at=now, completed_at=now)
            self._finish(settled)
            return

        # Only the review observer distinguishes a verdict nobody has read from
        # one already delivered; another service's observer has no such notion,
        # so it is neither asked the question nor made to carry the argument.
        # The signature is inspected rather than TypeError caught, which would
        # also swallow a genuine argument fault raised inside the observer.
        if _accepts_consumption(observer):
            # Whether an earlier task for this same subject and head already
            # took a verdict. Publication time cannot tell a stale answer from
            # one nobody has read: a reviewer that reviews a new pull request
            # unasked publishes before the request that follows it. The store
            # knows which it is, and the observer reads review evidence rather
            # than its own history, so the fact is supplied here.
            try:
                already_consumed = self._store.verdict_already_consumed(
                    task.service,
                    task.subject_reference,
                    task.requested_at.isoformat(),
                )
            except Exception:  # noqa: BLE001 - the store is injected; any failure reads the same
                # Unreadable history is not permission to reuse a verdict. The
                # stricter reading holds until the store can answer, so a
                # broken store delays a completion rather than inventing one.
                LOGGER.warning(
                    "Task history unreadable: treating verdict as consumed"
                )
                already_consumed = True
            observation = observer.observe(
                task.subject_reference,
                task.requested_at,
                already_consumed=already_consumed,
            )
        else:
            observation = observer.observe(
                task.subject_reference, task.requested_at
            )
        if observation.state in (TaskState.COMPLETED, TaskState.FAILED):
            resolved_subject = observation.subject_reference or task.subject_reference
            settled = replace(
                task,
                subject_reference=resolved_subject,
                state=observation.state,
                last_checked_at=observation.observed_at,
                completed_at=observation.observed_at,
            )
            self._finish(settled)
            return

        waiting = replace(
            task,
            state=observation.state,
            last_checked_at=observation.observed_at,
        )
        self._store.record(waiting)
        self._announce(
            task.conversation_id,
            self._payload(waiting, observation.observed_at),
        )

    def _finish(self, task: ExternalTask) -> None:
        attached = task.task_id in self._waiting
        self._announce(task.conversation_id, self._payload(task, task.completed_at))
        if not attached:
            self._completed(task)
        self._store.record(task, handed_over=attached)
        if attached:
            self._settled[task.task_id] = task
            self._lock.notify_all()

    @staticmethod
    def _payload(task: ExternalTask, at: datetime) -> dict[str, object]:
        return {
            "task_id": task.task_id,
            "state": task.state.value,
            "subject": _subject(task),
            "service": task.service,
            "elapsed_seconds": int(task.elapsed_seconds(at)),
        }


def _subject(task: ExternalTask) -> str:
    """The task's subject in a form someone reads.

    Identifiers only. A pull request number and an abbreviated revision are
    operational facts; nothing about what the review found passes through.
    """
    reference = task.subject_reference
    if reference.startswith("pull/") and "@" in reference:
        number, sha = reference[len("pull/"):].split("@", 1)
        return f"PR #{number} @ {sha[:7]}"
    return reference
