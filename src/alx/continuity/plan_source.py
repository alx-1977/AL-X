"""Run execution plans in the background, and offer their attentions to the Core.

Two small parts, and nothing else owns a plan:

- `PlanWorkers` runs each planned step the Core checkpointed on a background
  worker, outside the Core-turn lock, so a two-hour coding step leaves AL/X
  conversational and other plans moving. Only the step's durable in-flight
  identity lets its eventual result move the plan.
- `PlanAttentionSource` offers a plan that needs AL/X to the one occasion
  runner. Whether she is needed is read from the plan's own attention; the
  offer counts, the backoff and the cap live there too. Once automatic paid
  offers are exhausted the attention is blocked, one structural notice is
  published, and the plan stays where every Core turn shows it until she
  resolves it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from alx.contracts import run_core_worker
from alx.contracts.cognition import CognitionOrigin
from alx.contracts.continuity import CognitionOpportunity
from alx.contracts.records import GoalStatus

LOGGER = logging.getLogger(__name__)

# Automatic offers that may have reached a paid reasoning call before the
# attention is blocked. Durable, so a restart cannot extend it.
MAX_PAID_PLAN_OFFERS = 3
# The wait after an offer before the same attention is offered again: this,
# doubled per earlier offer, at most sixteen times.
PLAN_OFFER_BACKOFF_SECONDS = 300
_REFERENCE = "execution_plan:"


class PlanAttentionSource:
    """Offer each plan attention that is due, and block one that is exhausted."""

    def __init__(
        self,
        goals: Any,
        ledger: Any,
        enabled: bool = False,
        notify: Callable[[str, Mapping[str, Any]], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        max_paid_offers: int = MAX_PAID_PLAN_OFFERS,
        backoff_seconds: float = PLAN_OFFER_BACKOFF_SECONDS,
    ) -> None:
        self._goals = goals
        # The occasion ledger keeps each offer's spend and outcome for audit,
        # as for every occasion. It decides nothing about the plan.
        self._ledger = ledger
        self._enabled = enabled
        # Structural, content-free notice for the person's console: identifiers
        # and a state, never words in AL/X's voice.
        self._notify = notify or (lambda _conversation_id, _values: None)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max_paid_offers = max_paid_offers
        self._backoff = timedelta(seconds=backoff_seconds)
        # Blocked attentions already shown in this process, and where.
        self._notified: dict[tuple[str, str, int], str] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def due_opportunities(self) -> tuple[CognitionOpportunity, ...]:
        if not self._enabled:
            return ()
        now = self._clock()
        due = []
        for goal_id in self._goals.list_open_plan_goal_ids(needing_core=True):
            try:
                snapshot = self._goals.load(goal_id)
            except Exception as error:  # noqa: BLE001 - one goal must not hide the rest
                LOGGER.warning("Plan attention unreadable for goal %s: %s",
                               goal_id, type(error).__name__)
                continue
            plan = snapshot.state.execution_plan
            attention = None if plan is None else plan.attention
            if (attention is None or attention.blocked
                    or snapshot.state.status is not GoalStatus.ACTIVE
                    or (attention.next_offer_at is not None and attention.next_offer_at > now)):
                continue
            due.append(CognitionOpportunity(
                self._opportunity_id(goal_id, plan), CognitionOrigin.WORK_COMPLETED,
                now, snapshot.conversation_id, references=(f"{_REFERENCE}{goal_id}",),
            ))
        return tuple(due)

    @staticmethod
    def _opportunity_id(goal_id: str, plan: Any) -> str:
        attention = plan.attention
        return f"{_REFERENCE}{goal_id}:{plan.plan_id}:{attention.seq}:{attention.offers}"

    def owns(self, opportunity: CognitionOpportunity) -> bool:
        return any(item.startswith(_REFERENCE) for item in opportunity.references)

    def claim(self, opportunity: CognitionOpportunity) -> bool:
        """Count the offer on the plan before anything is spent.

        Counted as paid until the runner says the provider was never reached,
        so a crash during the turn counts against the cap rather than for it.
        """
        snapshot = self._current(opportunity, offers_delta=0)
        if snapshot is None:
            return False
        plan = snapshot.state.execution_plan
        attention = plan.attention
        now = self._clock()
        try:
            self._goals.replace(
                replace(snapshot.state, execution_plan=replace(plan, attention=replace(
                    attention, offers=attention.offers + 1,
                    paid_offers=attention.paid_offers + 1,
                    next_offer_at=now + self._backoff * (2 ** min(attention.offers, 4)),
                ))),
                snapshot.retention_until, snapshot.revision, snapshot.provenance,
            )
        except Exception as error:  # noqa: BLE001 - not claimed, offered again later
            LOGGER.warning("Plan attention could not be claimed: %s", type(error).__name__)
            return False
        try:
            self._ledger.record_created(opportunity)
        except Exception as error:  # noqa: BLE001 - the audit row, not ownership
            LOGGER.warning("Plan offer audit row not written: %s", type(error).__name__)
        return True

    def release(self, opportunity: CognitionOpportunity) -> None:
        """The turn never reached a provider: that offer cost nothing.

        Its backoff stands, so a budget refusal is not offered again on every
        tick; only the paid count is returned.
        """
        snapshot = self._current(opportunity, offers_delta=1)
        if snapshot is not None:
            plan = snapshot.state.execution_plan
            attention = plan.attention
            try:
                self._goals.replace(
                    replace(snapshot.state, execution_plan=replace(plan, attention=replace(
                        attention, paid_offers=max(0, attention.paid_offers - 1),
                    ))),
                    snapshot.retention_until, snapshot.revision, snapshot.provenance,
                )
            except Exception as error:  # noqa: BLE001 - counted as paid: the safe side
                LOGGER.warning("Plan offer could not be released: %s", type(error).__name__)
        try:
            self._ledger.release(opportunity.opportunity_id)
        except Exception as error:  # noqa: BLE001 - audit only
            LOGGER.warning("Plan offer audit row not released: %s", type(error).__name__)

    def mark_honoured(self, opportunity: CognitionOpportunity) -> None:
        # Whether she resolved the attention is the plan's own state. A turn
        # that left it standing simply leaves it to its next offer.
        pass

    def _current(self, opportunity: CognitionOpportunity, *, offers_delta: int) -> Any:
        """The goal, if the plan still holds the attention this offer named."""
        goal_id = next((item[len(_REFERENCE):] for item in opportunity.references
                        if item.startswith(_REFERENCE)), None)
        if goal_id is None:
            return None
        try:
            snapshot = self._goals.load(goal_id)
        except Exception as error:  # noqa: BLE001 - nothing to count against
            LOGGER.warning("Plan attention unreadable for goal %s: %s",
                           goal_id, type(error).__name__)
            return None
        plan = snapshot.state.execution_plan
        if plan is None or plan.attention is None:
            return None
        attention = plan.attention
        expected = (f"{_REFERENCE}{goal_id}:{plan.plan_id}:{attention.seq}:"
                    f"{attention.offers - offers_delta}")
        return snapshot if expected == opportunity.opportunity_id else None

    def settle(self) -> tuple[str, ...]:
        """Block exhausted attentions and keep each blocked one visible.

        Runs under the Core lock at the start of each tick. An attention whose
        paid offers reached the cap is blocked durably; every blocked one is
        announced once per process, so a restart shows it again, and one
        announced earlier and since resolved is announced as resolved. No
        reasoning call is involved in any of it.
        """
        blocked_now: dict[tuple[str, str, int], str] = {}
        changed = []
        for goal_id in self._goals.list_open_plan_goal_ids(needing_core=True):
            try:
                snapshot = self._goals.load(goal_id)
                plan = snapshot.state.execution_plan
                attention = None if plan is None else plan.attention
                if attention is None:
                    continue
                if not attention.blocked and attention.paid_offers >= self._max_paid_offers:
                    attention = replace(attention, blocked=True)
                    self._goals.replace(
                        replace(snapshot.state,
                                execution_plan=replace(plan, attention=attention)),
                        snapshot.retention_until, snapshot.revision, snapshot.provenance,
                    )
                    LOGGER.warning("Plan attention %s:%d blocked after %d paid offers",
                                   plan.plan_id, attention.seq, attention.paid_offers)
                    changed.append(goal_id)
                if attention.blocked:
                    blocked_now[(goal_id, plan.plan_id, attention.seq)] = (
                        snapshot.conversation_id)
                    if (goal_id, plan.plan_id, attention.seq) not in self._notified:
                        self._publish(snapshot.conversation_id, goal_id, plan.plan_id,
                                      attention.seq, "blocked", attention.reason)
            except Exception as error:  # noqa: BLE001 - one goal must not stop the rest
                LOGGER.warning("Plan attention could not be settled for goal %s: %s",
                               goal_id, type(error).__name__)
                # Unknown this tick: keep whatever was shown.
                blocked_now.update({key: value for key, value in self._notified.items()
                                    if key[0] == goal_id})
        for key, conversation_id in self._notified.items():
            if key not in blocked_now:
                self._publish(conversation_id, *key, "resolved", None)
        self._notified = blocked_now
        return tuple(changed)

    def _publish(self, conversation_id: str, goal_id: str, plan_id: str, seq: int,
                 state: str, reason: str | None) -> None:
        try:
            self._notify(conversation_id, {
                "goal_id": goal_id, "plan_id": plan_id, "attention_seq": seq,
                "state": state, "reason": reason,
            })
        except Exception as error:  # noqa: BLE001 - a console must not stop a tick
            LOGGER.warning("Plan attention notice failed: %s", type(error).__name__)


class PlanWorkers:
    """Dispatch checkpointed steps on background workers.

    The Core-turn lock is held only to checkpoint and to record: never across
    a dispatch. Each worker records its own result and starts the plan's next
    step, so a running plan does not wait for the next tick between steps.
    """

    def __init__(self, core: Any, core_turn_lock: asyncio.Lock,
                 attention: PlanAttentionSource | None = None) -> None:
        self._core = core
        self._lock = core_turn_lock
        self._attention = attention
        self._tasks: set[asyncio.Task] = set()

    async def advance(self) -> int:
        """One tick: reconcile, checkpoint due steps, start their workers."""
        async with self._lock:
            jobs = await run_core_worker(self._core.advance_due_plans)
            if self._attention is not None:
                await run_core_worker(self._attention.settle)
        for job in jobs:
            self._start(job)
        return len(jobs)

    async def drain(self) -> None:
        """Wait until no planned step is running here."""
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    def _start(self, job: Any) -> None:
        task = asyncio.ensure_future(self._run(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, job: Any) -> None:
        try:
            attempt = await asyncio.to_thread(self._core.run_planned_dispatch, job)
        except asyncio.CancelledError:
            # Shutdown. The checkpoint stays pending; the next process closes
            # it as interrupted and never replays it.
            raise
        except Exception as error:  # noqa: BLE001 - recorded as interrupted
            LOGGER.warning("Planned step for goal %s raised: %s",
                           job.goal_id, type(error).__name__)
            attempt = None
        try:
            async with self._lock:
                following = await run_core_worker(
                    self._core.finish_planned_dispatch, job, attempt)
        except Exception as error:  # noqa: BLE001 - restart recovery closes it
            # The result is not recorded. The checkpoint stays pending and is
            # closed as interrupted, never replayed.
            LOGGER.warning("Planned result for goal %s not recorded: %s",
                           job.goal_id, type(error).__name__)
            return
        for item in following:
            self._start(item)


__all__ = [
    "MAX_PAID_PLAN_OFFERS",
    "PLAN_OFFER_BACKOFF_SECONDS",
    "PlanAttentionSource",
    "PlanWorkers",
]
