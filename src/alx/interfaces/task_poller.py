"""Watch outstanding external work for the life of the process.

A service handed work does not stop when a browser closes, so what watches it
lives beside the transport rather than inside a session, exactly as the mail
scan and the due-cognition tick do.

This decides nothing. It asks an observer what it can see, records the state,
writes a terminal line, and sleeps. When a result appears it stops watching and
raises one cognition opportunity, so the Core evaluates the result through the
one ingress that already exists. It never requests a review, retries, spends,
fixes or merges: it imports nothing that could, and a test asserts that.

Terminal lines carry identifiers, states and durations only, which is what
D-012 permits. No finding text passes through here, because none reaches here.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
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
        """One look at every outstanding task."""
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
        now = datetime.now(UTC)
        if observer is None:
            unavailable = replace(
                task,
                state=TaskState.OBSERVER_UNAVAILABLE,
                last_checked_at=now,
            )
            self._store.record(unavailable)
            self._announce(
                task.conversation_id,
                self._payload(unavailable, now),
            )
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
            # Announce and wake first, and only then record the outcome. A
            # callback that fails after the write would leave a task settled
            # in the store and never reported, so the result would be lost
            # rather than retried on the next tick.
            self._announce(
                task.conversation_id,
                self._payload(settled, observation.observed_at),
            )
            # The Core evaluates the result. This does not read it.
            self._completed(settled)
            self._store.record(settled)
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
