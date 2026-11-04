"""Run execution plans in the background, and offer their attentions to the Core.

Two small parts, and nothing else owns a plan:

- `PlanWorkers` runs each planned step the Core checkpointed on a background
  worker, outside the Core-turn lock, so a two-hour coding step leaves AL/X
  conversational and other plans moving. Only the step's durable in-flight
  identity lets its eventual result move the plan.
- `PlanAttentionSource` offers a plan that needs AL/X to the one occasion
  runner. Whether she is needed is read from the plan's own attention; the
  offer counts, the backoff and the cap live there too. Once automatic paid
  offers are exhausted the attention is blocked, one durable conversation note is
  stored, and the plan stays where every Core turn shows it until she
  resolves it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

from alx.contracts import run_core_worker
from alx.contracts.cognition import CognitionOrigin
from alx.contracts.continuity import CognitionOpportunity
from alx.contracts.records import GoalStatus, PlanStopNote

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
        notify: Callable[[PlanStopNote], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        # The autonomous spend ledger. Its durable record that an occasion
        # reached the provider is the only thing that makes an offer paid.
        spend: Any = None,
        # Whether a goal's attention can be shown with its evidence yet.
        ready: Callable[[str], bool] | None = None,
        max_paid_offers: int = MAX_PAID_PLAN_OFFERS,
        backoff_seconds: float = PLAN_OFFER_BACKOFF_SECONDS,
    ) -> None:
        self._goals = goals
        # The occasion ledger keeps each offer's spend and outcome for audit,
        # as for every occasion. It decides nothing about the plan.
        self._ledger = ledger
        self._enabled = enabled
        self._notify = notify
        self._clock = clock or (lambda: datetime.now(UTC))
        self._spend = spend
        self._ready = ready or (lambda _goal_id: True)
        self._max_paid_offers = max_paid_offers
        self._backoff = timedelta(seconds=backoff_seconds)

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
            try:
                ready = self._ready(goal_id)
            except Exception as error:  # noqa: BLE001 - not offered this tick
                LOGGER.warning("Plan attention readiness unknown for goal %s: %s",
                               goal_id, type(error).__name__)
                ready = False
            if not ready:
                continue
            due.append(CognitionOpportunity(
                self._opportunity_id(goal_id, plan), CognitionOrigin.WORK_COMPLETED,
                now, snapshot.plan_conversation_id, references=(f"{_REFERENCE}{goal_id}",),
            ))
        return tuple(due)

    @staticmethod
    def _opportunity_id(goal_id: str, plan: Any, offer: int | None = None) -> str:
        attention = plan.attention
        number = attention.offers if offer is None else offer
        return f"{_REFERENCE}{goal_id}:{plan.plan_id}:{attention.seq}:{number}"

    def owns(self, opportunity: CognitionOpportunity) -> bool:
        return any(item.startswith(_REFERENCE) for item in opportunity.references)

    def claim(self, opportunity: CognitionOpportunity) -> bool:
        """Record the offer, and its backoff, before anything is spent.

        Whether it was paid is not decided here or from how the turn ended:
        `settle` reads it from the spend ledger, which records that the
        provider was about to be reached before the call is made.
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
        """The turn did not happen. Its backoff stands; only the audit row goes."""
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
        """Block exhausted attentions and idempotently retain their conversation note.

        The conversation turn is the durable notification identity, not an
        independently persisted sent flag. Failed storage is retried next tick.
        Usage-limit refusals do not count as paid offers and never block here.
        """
        changed = []
        for goal_id in self._goals.list_open_plan_goal_ids(needing_core=True):
            try:
                snapshot = self._goals.load(goal_id)
                plan = snapshot.state.execution_plan
                attention = None if plan is None else plan.attention
                if attention is None:
                    continue
                paid = self._paid_offers(goal_id, plan)
                if paid != attention.paid_offers or (
                        not attention.blocked and paid >= self._max_paid_offers):
                    attention = replace(attention, paid_offers=paid,
                                        blocked=attention.blocked or paid >= self._max_paid_offers)
                    self._goals.replace(
                        replace(snapshot.state,
                                execution_plan=replace(plan, attention=attention)),
                        snapshot.retention_until, snapshot.revision, snapshot.provenance,
                    )
                    if attention.blocked:
                        LOGGER.warning("Plan attention %s:%d blocked after %d paid offers",
                                       plan.plan_id, attention.seq, attention.paid_offers)
                    changed.append(goal_id)
            except Exception as error:  # noqa: BLE001 - one goal must not stop the rest
                LOGGER.warning("Plan attention could not be settled for goal %s: %s",
                               goal_id, type(error).__name__)
        if self._notify is not None:
            for note in self._goals.pending_plan_stop_notes():
                try:
                    self._notify(note)
                    self._goals.acknowledge_plan_stop_note(note)
                except Exception as error:  # noqa: BLE001 - retained for next tick
                    LOGGER.warning("Plan stop note not stored: %s", type(error).__name__)
        return tuple(changed)

    def _paid_offers(self, goal_id: str, plan: Any) -> int:
        """How many of this attention's offers were answered by a provider, durably.

        Answered, not merely dispatched: a refused call, such as one made
        while the usage limit is reached, gave her no turn, so it does not
        count towards the cap.
        """
        if self._spend is None:
            return plan.attention.paid_offers
        return sum(
            1 for offer in range(plan.attention.offers)
            if self._spend.dispatch_answered(self._opportunity_id(goal_id, plan, offer))
        )



class PlanWorkers:
    """Dispatch checkpointed steps on background workers.

    The Core-turn lock is held only to checkpoint, to cross the dispatch
    boundary, and to record: never across a dispatch. Each worker records
    its own result and starts the plan's next step, so a running plan does
    not wait for the next tick between steps.

    Shutdown (`stop`) is the same rule a Core turn already follows: stores
    close only after the work using them has reached a durable boundary.
    A worker that has not crossed the dispatch boundary declines to: its
    checkpoint stays unstarted and is dropped and run again after restart.
    A worker whose call has started is asked to cancel where its capability
    supports that, and is waited for until its result is recorded.
    """

    def __init__(self, core: Any, core_turn_lock: asyncio.Lock,
                 attention: PlanAttentionSource | None = None,
                 cancel_dispatch: Callable[[Any], None] | None = None,
                 reconcile_replies: Callable[[], Any] | None = None) -> None:
        self._core = core
        self._lock = core_turn_lock
        self._attention = attention
        # A capability's own cancel, where one exists (a coding job). Asked,
        # never relied on: shutdown still waits for the call to return.
        self._cancel_dispatch = cancel_dispatch or (lambda _job: None)
        # Stores a finish or cancel reply a failure kept from the conversation.
        self._reconcile_replies = reconcile_replies
        self._tasks: set[asyncio.Task] = set()
        # Workers past the dispatch boundary, by task: these are waited for.
        self._started: dict[asyncio.Task, Any] = {}
        self._stopping = False

    async def advance(self) -> int:
        """One tick: reconcile, checkpoint due steps, start their workers."""
        if self._stopping:
            return 0
        async with self._lock:
            if self._reconcile_replies is not None:
                await run_core_worker(self._reconcile_replies)
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
            # Gathering finished tasks need not yield to the loop, so their
            # done callbacks may not have removed them yet.
            self._tasks.difference_update(
                [task for task in self._tasks if task.done()])

    async def stop(self) -> None:
        """Stop starting work, and wait for started work to be recorded."""
        # Set at once, so a worker still waiting for its boundary never
        # crosses it. Then read the started calls under the lock every
        # boundary is crossed under: a worker inside `begin_planned_dispatch`
        # finishes and registers first, so it is asked to cancel too.
        self._stopping = True
        async with self._lock:
            started = tuple(self._started.values())
        for job in started:
            try:
                self._cancel_dispatch(job)
            except Exception as error:  # noqa: BLE001 - still waited for
                LOGGER.warning("Planned step cancel request failed: %s",
                               type(error).__name__)
        await self.drain()

    def _start(self, job: Any) -> None:
        if self._stopping:
            return
        task = asyncio.ensure_future(self._run(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, job: Any) -> None:
        task = asyncio.current_task()
        # The dispatch boundary, under the lock: a cancel or replacement that
        # committed first means the call is never made.
        async with self._lock:
            if self._stopping:
                # Never crossed: restart drops this checkpoint and runs it.
                return
            try:
                started = await run_core_worker(self._core.begin_planned_dispatch, job)
            except Exception as error:  # noqa: BLE001 - the next tick reconciles it
                LOGGER.warning("Planned step for goal %s could not start: %s",
                               job.goal_id, type(error).__name__)
                return
            if started is None:
                return
            if task is not None:
                self._started[task] = started
        try:
            try:
                attempt = await asyncio.to_thread(self._core.run_planned_dispatch, started)
            except asyncio.CancelledError:
                # Not sent by `stop`, which waits for a started call. Only a
                # cancelled runtime gets here; restart records it as interrupted.
                raise
            except Exception as error:  # noqa: BLE001 - recorded as interrupted
                LOGGER.warning("Planned step for goal %s raised: %s",
                               job.goal_id, type(error).__name__)
                attempt = None
            try:
                async with self._lock:
                    following = await run_core_worker(partial(
                        self._core.finish_planned_dispatch, started, attempt,
                        continue_plan=not self._stopping))
            except Exception as error:  # noqa: BLE001 - restart recovery closes it
                # The result is not recorded. The checkpoint stays pending and
                # is closed as interrupted, never replayed.
                LOGGER.warning("Planned result for goal %s not recorded: %s",
                               job.goal_id, type(error).__name__)
                return
        finally:
            if task is not None:
                self._started.pop(task, None)
        for item in following:
            self._start(item)


__all__ = [
    "MAX_PAID_PLAN_OFFERS",
    "PLAN_OFFER_BACKOFF_SECONDS",
    "PlanAttentionSource",
    "PlanWorkers",
]
