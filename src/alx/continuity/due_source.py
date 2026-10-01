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

from alx.contracts import run_core_worker

LOGGER = logging.getLogger(__name__)


class DueCognitionSource:
    """The tick. It notices matured requests and nothing else."""

    def __init__(
        self,
        source: Any,
        runner: Any,
        core_turn_lock: asyncio.Lock,
        interval_seconds: float,
        # Lifts holds on occasions that were too large for the input bound,
        # so a changed bound or a lapsed hold is offered again. One ledger
        # update; it starts no Core turn.
        reopen: Any = None,
        advance_plans: Any = None,
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
        self._reopen = reopen
        self._advance_plans = advance_plans

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
        # Occasions held because they did not fit are released first when
        # their hold has lapsed, so they are offered in this same tick.
        if self._advance_plans is not None:
            async with self._core_turn_lock:
                await run_core_worker(self._advance_plans)
        if self._reopen is not None:
            await asyncio.to_thread(self._reopen)
        run = 0
        # Offered once each per tick. An occasion the runner declines without
        # holding a claim is due again at once, and must wait for the next
        # tick rather than spin this one.
        offered: set[str] = set()
        while True:
            # Asked again before every turn, never once for the whole tick. A
            # turn can change what is due: AL/X may withdraw a revisit she now
            # sees is superseded, or close work another occasion covered. A
            # list taken before the first turn would still run those, which is
            # a Core turn for a request she had already withdrawn.
            opportunities = await asyncio.to_thread(self._source.due_opportunities)
            opportunity = next(
                (item for item in opportunities
                 if item.opportunity_id not in offered),
                None,
            )
            if opportunity is None:
                return run
            offered.add(opportunity.opportunity_id)
            # Held across the whole turn, exactly as the voice path holds it.
            async with self._core_turn_lock:
                # Shielded because cancelling the await would unwind this
                # coroutine and release the lock while the worker thread kept
                # writing to stores that shutdown is about to close. The turn
                # is short and reaches its own durable boundary; letting it
                # finish is the only way the lock can mean what it says.
                # The lock is held until the worker itself finishes, so
                # shutdown cannot close stores under a turn that is still
                # writing. Shared with every other kind of turn.
                if await run_core_worker(self._runner.run_one, opportunity):
                    run += 1
