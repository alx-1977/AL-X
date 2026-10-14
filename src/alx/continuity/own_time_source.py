"""Turn AL/X's own time of day into a cognition occasion.

D-024, amended 2026-10-14: Friedl gave her time of her own, "once a day,
mid-morning" to begin with, and hers to change. Each time of day she has set
comes round once a day in the local time of the machine she runs on, and is
offered to the Core as an occasion of its own origin. That is the whole of
this module.

Like its siblings it decides nothing. It does not look at her goals, her
notebook or her mail, it does not suggest what the time is for, and it does
not judge whether she used it. Whether she thinks about anything at all, and
what, is hers.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, time, tzinfo
from typing import Any

from alx.contracts.cognition import CognitionOrigin
from alx.contracts.continuity import CognitionOpportunity

# Her own thread, as a restart has its own. Her own time belongs to no
# person's conversation and to no mail thread.
OWN_TIME_CONVERSATION_ID = "own-time"
_PREFIX = "own-time:"


class OwnTimeSource:
    """The one place her own time of day becomes an occasion."""

    def __init__(
        self,
        store: Any,
        ledger: Any,
        enabled: bool = False,
        clock: Callable[[], datetime] | None = None,
        # The local time zone her times of day are in: the machine's own,
        # unless a test names one.
        local_zone: tzinfo | None = None,
    ) -> None:
        self._store = store
        self._ledger = ledger
        # Off unless the runtime may think unprompted, as for every sibling.
        self._enabled = enabled
        self._clock = clock or (lambda: datetime.now(UTC))
        self._local_zone = local_zone

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _slots_today(self) -> list[tuple[str, datetime]]:
        """Today's times that have come round, as (occasion id, local time).

        Today in the machine's local time. A time she set after it had already
        passed today first comes round tomorrow, so changing the schedule never
        starts an occasion by itself.
        """
        now = self._clock().astimezone(self._local_zone)
        times, updated_at = self._store.own_time()
        slots = []
        for value in times:
            hour, minute = (int(part) for part in value.split(":"))
            at = datetime.combine(now.date(), time(hour, minute), tzinfo=now.tzinfo)
            if at > now:
                continue
            if updated_at is not None and updated_at > at:
                continue
            slots.append((f"{_PREFIX}{now.date().isoformat()}:{value}", at))
        return slots

    def due_opportunities(self) -> tuple[CognitionOpportunity, ...]:
        """Each of today's own times that has come round and not been offered."""
        if not self._enabled:
            return ()
        return tuple(
            CognitionOpportunity(
                opportunity_id=opportunity_id,
                origin=CognitionOrigin.OWN_TIME,
                arose_at=at.astimezone(UTC),
                conversation_id=OWN_TIME_CONVERSATION_ID,
                references=(f"own_time:{opportunity_id[len(_PREFIX):]}",),
            )
            for opportunity_id, at in self._slots_today()
            if not self._ledger.exists(opportunity_id)
        )

    def recover(self, spend: Any = None) -> tuple[str, ...]:
        """Close own-time occasions a stopped process left claimed.

        One that reached dispatch is kept for inspection and never replayed;
        one that did not is released. A released one from an earlier day is
        never offered again, because only today's times are.
        """
        reclaimed: list[str] = []
        for row in self._ledger.unfinished():
            opportunity_id = row["opportunity_id"]
            if not opportunity_id.startswith(_PREFIX):
                continue
            if spend is not None and spend.dispatch_started(opportunity_id):
                self._ledger.mark_unreconciled(opportunity_id)
                continue
            self._ledger.release(opportunity_id)
            reclaimed.append(opportunity_id)
        return tuple(reclaimed)

    def owns(self, opportunity: CognitionOpportunity) -> bool:
        return opportunity.opportunity_id.startswith(_PREFIX)

    def claim(self, opportunity: CognitionOpportunity) -> bool:
        return self._ledger.record_created(opportunity)

    def release(self, opportunity: CognitionOpportunity) -> None:
        self._ledger.release(opportunity.opportunity_id)

    def mark_honoured(self, opportunity: CognitionOpportunity) -> None:
        """Nothing to close: the ledger row is the whole record of the occasion."""
