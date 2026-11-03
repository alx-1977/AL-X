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

Offers are bounded. One answered offer per goal revision, because a turn that
changes nothing must not be offered again for the same state, and at most
`max_offers` answered offers per goal in a rolling window. An offer refused at
a usage limit gave her no turn: the same state is offered again after a
backoff. The last offer says so in its references, so she knows to
tell Friedl what is holding the goal rather than leave it silently.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from alx.contracts.cognition import CognitionOrigin
from alx.contracts.continuity import CognitionOpportunity

LOGGER = logging.getLogger(__name__)

_PREFIX = "idle_goal:"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# How long a goal must sit unchanged before it is offered. Long enough that a
# turn which is about to schedule its own work is never interrupted.
IDLE_GOAL_SECONDS = 180
# Offers per goal in the window. The last one asks her to say what she needs.
MAX_IDLE_GOAL_OFFERS = 3
IDLE_GOAL_WINDOW_HOURS = 12
# The wait before a refused offer is made again: this, doubled per refusal of
# the same state, at most sixteen times. As for plan attentions.
IDLE_GOAL_BACKOFF_SECONDS = 300


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
        # The autonomous spend ledger: whether an offer was answered.
        spend: Any = None,
        backoff_seconds: float = IDLE_GOAL_BACKOFF_SECONDS,
    ) -> None:
        self._goals = goals
        self._ledger = ledger
        # Off unless the runtime may think unprompted, as for every sibling.
        self._enabled = enabled
        self._clock = clock or (lambda: datetime.now(UTC))
        self._idle = timedelta(seconds=idle_seconds)
        self._max_offers = max_offers
        self._window = timedelta(hours=window_hours)
        self._spend = spend
        self._backoff = timedelta(seconds=backoff_seconds)

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
            try:
                snapshot = self._goals.load(goal_id)
                offer = self._next_offer(goal_id, snapshot.revision, now)
            except Exception as error:  # noqa: BLE001 - one goal must not hide the rest
                LOGGER.warning("Idle goal %s could not be offered: %s",
                               goal_id, type(error).__name__)
                continue
            if offer is None:
                continue
            opportunity_id, number = offer
            due.append(CognitionOpportunity(
                opportunity_id=opportunity_id,
                origin=CognitionOrigin.WORK_COMPLETED,
                arose_at=now,
                conversation_id=snapshot.conversation_id,
                references=(
                    f"{_PREFIX}{goal_id}",
                    f"idle_goal_offer:{number}/{self._max_offers}",
                ),
            ))
        return tuple(due)

    def _answered(self, opportunity_id: str) -> bool:
        """Whether she had a turn on this offer. Without a spend ledger, any recorded offer did."""
        return self._spend is None or self._spend.dispatch_answered(opportunity_id)

    def _next_offer(self, goal_id: str, revision: int, now: datetime) -> tuple[str, int] | None:
        """The next offer for this goal's current state, as (identity, number), or None.

        Only answered offers count: one refused at a usage limit gave her no
        turn, so the same state is offered again after a backoff instead of
        being spent.
        """
        offers = self._ledger.recorded(f"{_PREFIX}{goal_id}:", now - self._window)
        answered = [identity for identity, _at in offers if self._answered(identity)]
        if len(answered) >= self._max_offers:
            return None
        state_prefix = f"{_PREFIX}{goal_id}:{revision}:"
        # Every offer for this state, not only the window's: the next retry
        # number must never repeat an identity the ledger already holds.
        this_state = list(self._ledger.recorded(state_prefix, _EPOCH))
        if any(self._answered(identity) for identity, _at in this_state):
            return None
        if this_state:
            refused = len(this_state)
            backoff = self._backoff * (2 ** min(refused - 1, 4))
            if this_state[-1][1] + backoff > now:
                return None
        return f"{state_prefix}{len(this_state)}", len(answered) + 1

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
