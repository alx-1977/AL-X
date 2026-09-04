"""Run the mechanical mail scan for the life of the process.

Mail discovery and reconciliation used to live inside a voice exchange, so
closing the browser stopped them: no session meant no scanning, and on
2026-09-04 an observation stayed stranded for an hour partly because nothing
was looking. The mailbox is not a property of whether anyone is currently
listening, so the scan that watches it lives beside the transport rather than
inside it, exactly as the due-cognition tick does.

This decides nothing. It sleeps, calls the one existing scanner, and sleeps
again. It does not know what mail is, whether any of it matters, or that AL/X
exists. Discovery writes durable observations; carrying them to her remains the
delivery path's job, so a scan makes zero Core calls and zero provider calls
however much mail it finds.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

LOGGER = logging.getLogger(__name__)


class MailPoller:
    """The mail tick. It runs the scanner and nothing else."""

    def __init__(
        self,
        source: Any,
        interval_seconds: float,
        store_lock: asyncio.Lock,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._source = source
        self._interval_seconds = interval_seconds
        # Held across a scan so shutdown cannot close the observation store
        # while one is writing to it. The same reason the Core-turn lock is
        # taken during teardown: cancelling a coroutine does not stop the
        # worker thread already running inside `asyncio.to_thread`.
        self._store_lock = store_lock

    async def run(self) -> None:
        """Tick for the life of the process."""
        while True:
            await asyncio.sleep(self._interval_seconds)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # A failed scan is not a failed runtime. The mailbox is still
                # there next time, and the cursor has not moved past anything
                # this scan did not manage to record.
                LOGGER.warning("Mail scan failed: %s", error)

    async def tick(self) -> None:
        """One scan, run so that shutdown cannot close the store beneath it.

        Two things must both hold once cancellation has been requested: the
        in-flight scan finishes before teardown closes the store, and the
        poller then stops. They pull in opposite directions, because waiting
        for a worker ordinarily means taking its outcome, and a scan that fails
        while we are stopping would otherwise report an ordinary scan failure
        to `run`, which resumes polling. Shutdown would then never complete.

        So completion and result are separated. `asyncio.wait` waits for the
        worker to finish without ever retrieving what it finished with; the
        exception is retrieved only to be logged, and the CancelledError is
        re-raised regardless. Outside cancellation nothing changes: the scan's
        outcome propagates to `run` exactly as before.
        """
        async with self._store_lock:
            worker = asyncio.ensure_future(asyncio.to_thread(self._source.scan))
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                # Wait for the worker to complete, and deliberately not for
                # its outcome. `await worker` would raise the scan's own
                # exception here and carry the cancellation away with it,
                # which is how a failing scan once made the poller
                # unstoppable: `run` saw an ordinary scan failure and polled
                # again, so shutdown during a mailbox outage never finished.
                #
                # `gather(return_exceptions=True)` returns once the scan has
                # finished, whether it succeeded or raised, and hands back a
                # value nothing here reads. Marking the failure handled also
                # keeps asyncio from rendering the exception and its traceback
                # at collection, which would leak exactly the private runtime
                # state D-012 exists to keep out of the logs.
                #
                # Either way the cancellation below is what leaves this frame,
                # and the store stays locked until the scan is done.
                await asyncio.gather(worker, return_exceptions=True)
                raise
