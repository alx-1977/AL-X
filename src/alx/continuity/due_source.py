"""Notice that a future cognition AL/X asked for has matured.

This is the whole of Phase 8's mechanism, and it is deliberately almost
nothing. It sleeps, asks the store whether any `not_before` has passed, and
hands whatever it finds to the one runner. It decides nothing.

The interval is a mechanical noticing interval, not a cognition cadence. It
means a matured request may be noticed up to that long afterwards; it does not
mean AL/X thinks that often. When nothing is due — and when the master switch
is off — a tick makes zero Core calls, zero claims, zero reservations and zero
provider calls. If she never asks for another occasion, this ticks forever and
invokes her never.

It lives for the life of the process rather than the life of a voice
connection, because D-024 says she is continuously present while the runtime is
running. Tying her cognition to whether someone is currently listening would
make her continuity a property of Friedl's attention.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

LOGGER = logging.getLogger(__name__)


class DueCognitionSource:
    """The tick. It notices matured requests and nothing else."""

    def __init__(
        self,
        source: Any,
        runner: Any,
        core_turn_lock: asyncio.Lock,
        interval_seconds: float,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._source = source
        self._runner = runner
        # The same lock the voice transport holds, so an autonomous turn and a
        # person turn can never run at once. Not a second lock: a duplicate
        # would serialize each path against itself and neither against the
        # other, which is worse than having none.
        self._core_turn_lock = core_turn_lock
        self._interval_seconds = interval_seconds

    async def run(self) -> None:
        """Tick for the life of the process."""
        while True:
            await asyncio.sleep(self._interval_seconds)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # A failed tick is not a failed runtime. The next one tries
                # again, and the request stays pending meanwhile.
                LOGGER.warning("Due-cognition tick failed: %s", error)

    async def tick(self) -> int:
        """One check. Returns how many occasions were run.

        Costs nothing when nothing is due: `due_opportunities` returns an empty
        tuple when the master switch is off or no `not_before` has matured, and
        this never reaches the runner.
        """
        opportunities = await asyncio.to_thread(self._source.due_opportunities)
        if not opportunities:
            return 0
        run = 0
        for opportunity in opportunities:
            # Held across the whole turn, exactly as the voice path holds it.
            async with self._core_turn_lock:
                # Shielded because cancelling the await would unwind this
                # coroutine and release the lock while the worker thread kept
                # writing to stores that shutdown is about to close. The turn
                # is short and reaches its own durable boundary; letting it
                # finish is the only way the lock can mean what it says.
                turn = asyncio.shield(
                    asyncio.to_thread(self._runner.run_one, opportunity)
                )
                try:
                    completed = await turn
                except asyncio.CancelledError:
                    # Shutdown arrived mid-turn. The shielded work is still
                    # running against stores that are about to close, so wait
                    # for its durable boundary before letting the cancellation
                    # propagate. Awaiting a shielded task after cancellation is
                    # the only point where the lock can still be held.
                    await asyncio.gather(turn, return_exceptions=True)
                    raise
                if completed:
                    run += 1
        return run
