"""Offer an active goal back to AL/X when nothing is carrying its work forward.

She thinks only when an occasion wakes her. A turn that ends with outstanding
work but starts no plan, job or revisit leaves that goal active with nothing
that will ever wake her for it. On 2026-11-02 and 2026-11-03 that happened
three times: each time she said what she would do next, and it waited until
Friedl asked.

This source closes that gap and decides nothing else. It reads storage facts
(active, outstanding work, no blocker, no unfinished plan, unchanged for a
while) and offers the goal. Whether the work is still worth doing, how to carry
it forward, or that it should be closed, is hers to decide in the turn.

Offers are bounded. One per goal revision, because a turn that changes nothing
must not be offered again for the same state, and at most `max_offers` per goal
in a rolling window. The last offer says so in its references, so she knows to
tell Friedl what is holding the goal rather than leave it silently.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from alx.contracts.cognition import CognitionOrigin
from alx.contracts.continuity import CognitionOpportunity

_PREFIX = "idle_goal:"
# How long a goal must sit unchanged before it is offered. Long enough that a
# turn which is about to schedule its own work is never interrupted.
IDLE_GOAL_SECONDS = 180
# Offers per goal in the window. The last one asks her to say what she needs.
MAX_IDLE_GOAL_OFFERS = 3
IDLE_GOAL_WINDOW_HOURS = 12


class IdleGoalSource:
    """The one place an idle active goal becomes an occasion."""

    def __init__(
        self,
        goals: Any,
        ledger: Any,
        enabled: bool = False,
        clock: Callable[[], datetime] | None = None,
        idle_seconds: float = IDLE_GOAL_SECONDS,
        max_offers: int = MAX_IDLE_GOAL_OFFERS,
        window_hours: float = IDLE_GOAL_WINDOW_HOURS,
    ) -> None:
        self._goals = goals
        self._ledger = ledger
        # Off unless the runtime may think unprompted, as for every sibling.
        self._enabled = enabled
        self._clock = clock or (lambda: datetime.now(UTC))
        self._idle = timedelta(seconds=idle_seconds)
        self._max_offers = max_offers
        self._window = timedelta(hours=window_hours)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def due_opportunities(self) -> tuple[CognitionOpportunity, ...]:
        if not self._enabled:
            return ()
        # UTC, because storage compares times as text.
        now = self._clock().astimezone(UTC)
        due = []
        for goal_id in self._goals.list_idle_goal_ids(now - self._idle):
            offered = self._ledger.count_recorded(f"{_PREFIX}{goal_id}:", now - self._window)
            if offered >= self._max_offers:
                continue
            snapshot = self._goals.load(goal_id)
            opportunity_id = f"{_PREFIX}{goal_id}:{snapshot.revision}"
            if self._ledger.exists(opportunity_id):
                continue
            due.append(CognitionOpportunity(
                opportunity_id=opportunity_id,
                origin=CognitionOrigin.WORK_COMPLETED,
                arose_at=now,
                conversation_id=snapshot.conversation_id,
                references=(
                    f"{_PREFIX}{goal_id}",
                    f"idle_goal_offer:{offered + 1}/{self._max_offers}",
                ),
            ))
        return tuple(due)

    def recover(self, spend: Any = None) -> tuple[str, ...]:
        """Release offers a stopped process left claimed before they reached a provider."""
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
        """Nothing to close: the goal's own state says whether she moved it."""
