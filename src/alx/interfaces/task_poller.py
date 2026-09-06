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
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from alx.contracts.task import ExternalTask, TaskState

LOGGER = logging.getLogger(__name__)


def _clock(seconds: float) -> str:
    """mm:ss, for a line someone reads rather than parses."""
    whole = int(max(0.0, seconds))
    return f"{whole // 60:02d}:{whole % 60:02d}"


class TaskPoller:
    """The external-task tick. It observes and reports, and nothing else."""

    def __init__(
        self,
        store: Any,
        observers: dict[str, Any],
        interval_seconds: float,
        announce: Callable[[str, str], None],
        completed: Callable[[ExternalTask], None],
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

    async def run(self) -> None:
        """Tick for the life of the process."""
        while True:
            await asyncio.sleep(self._interval_seconds)
            try:
                await asyncio.to_thread(self.tick)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # A failed look is not a failed runtime. The task is still
                # outstanding, and the next tick looks again.
                LOGGER.warning(
                    "External task check failed: %s", type(error).__name__
                )

    def tick(self) -> None:
        """One look at every outstanding task."""
        for task in self._store.outstanding():
            self._check(task)

    def _check(self, task: ExternalTask) -> None:
        observer = self._observers.get(task.service)
        now = datetime.now(UTC)
        if observer is None:
            # Nothing can see this task's service, so nothing can say where it
            # stands. Recorded as unknown rather than left looking outstanding.
            self._store.record(
                replace(task, state=TaskState.STATUS_UNKNOWN, last_checked_at=now)
            )
            return

        observation = observer.observe(task.subject_reference, task.requested_at)
        if observation.state is TaskState.COMPLETED:
            settled = replace(
                task,
                state=TaskState.COMPLETED,
                last_checked_at=observation.observed_at,
                completed_at=observation.observed_at,
            )
            # Announce and wake first, and only then record completion. A
            # callback that fails after the write would leave a task settled
            # in the store and never reported, so the result would be lost
            # rather than retried on the next tick.
            self._announce(
                task.conversation_id,
                f"Review received · {_subject(task)}",
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
        elapsed = _clock(task.elapsed_seconds(observation.observed_at))
        if observation.state is TaskState.STATUS_UNKNOWN:
            self._announce(
                task.conversation_id, f"Status unknown · {_subject(task)} · {elapsed}"
            )
        else:
            self._announce(task.conversation_id, f"Still waiting · {elapsed}")


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
