"""Offer a completed or interrupted execution plan to the existing Core runner.

Every continuation this source offers and the runner claims ends in exactly
one of three ways:

- handled: a Core decision that saw it answered it, and the plan says so;
- released: its turn ended without reaching the provider, so offering the
  same continuation again costs nothing that was not already refused;
- retried: its turn reached the provider and still left the plan standing, so
  the same identity is never replayed; a new generation is offered instead,
  at most MAX_CONTINUATION_GENERATION times. After that the plan stays where
  any turn that selects its goal can see it, and no occasion repeats it.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from alx.contracts.cognition import CognitionOrigin
from alx.contracts.continuity import CognitionOpportunity

LOGGER = logging.getLogger(__name__)

# Paid retries of one continuation, after its first attempt. Shared by the
# settle step and by startup recovery, so restarts cannot extend it.
MAX_CONTINUATION_GENERATION = 2
# How long a released continuation waits before it is offered again. A turn
# refused before reasoning, such as by an exhausted execution budget, would
# otherwise be refused again on every tick.
RELEASE_RETRY_SECONDS = 300
# Ledger outcomes that mean the claiming turn has not finished yet.
_IN_PROGRESS = frozenset({"created", "reserved"})


class PlanContinuationSource:
    def __init__(self, goals, ledger, enabled: bool = False, spend=None,
                 clock=None, retry_seconds: float = RELEASE_RETRY_SECONDS) -> None:
        self._goals = goals
        self._ledger = ledger
        self._enabled = enabled
        # The durable record of whether an occasion's call reached the
        # provider. Without it, a turn is assumed to have reached it.
        self._spend = spend
        self._clock = clock or (lambda: datetime.now(UTC))
        self._retry = timedelta(seconds=retry_seconds)
        self._not_before: dict[str, datetime] = {}
        self._exhausted_reported: set[str] = set()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def due_opportunities(self) -> tuple[CognitionOpportunity, ...]:
        if not self._enabled:
            return ()
        result = []
        now = self._clock()
        for goal_id in self._goals.list_open_plan_goal_ids():
            try:
                snapshot = self._goals.load(goal_id)
            except Exception as error:  # noqa: BLE001 - one goal must not hide the rest
                LOGGER.warning("Plan continuation unreadable for goal %s: %s",
                               goal_id, type(error).__name__)
                continue
            if snapshot.retention_until <= now:
                continue
            plan = snapshot.state.execution_plan
            if plan is None or plan.status not in {"needs_core", "completed"}:
                continue
            identifier = self._opportunity_id(snapshot.state.goal_id, plan)
            if self._ledger.exists(identifier) or self._not_before.get(identifier, now) > now:
                continue
            result.append(CognitionOpportunity(
                identifier, CognitionOrigin.WORK_COMPLETED,
                now, snapshot.conversation_id,
                references=(
                    f"execution_plan:{snapshot.state.goal_id}",
                    f"execution_plan_id:{plan.plan_id}",
                    f"execution_plan_cursor:{plan.cursor}",
                    f"execution_plan_generation:{plan.continuation_generation}",
                ),
            ))
        return tuple(result)

    @staticmethod
    def _opportunity_id(goal_id, plan) -> str:
        base = f"execution_plan:{goal_id}:{plan.plan_id}:{plan.cursor}"
        if plan.continuation_generation:
            return f"{base}:{plan.continuation_generation}"
        return base

    def owns(self, opportunity: CognitionOpportunity) -> bool:
        return any(ref.startswith("execution_plan:") for ref in opportunity.references)

    def claim(self, opportunity: CognitionOpportunity) -> bool:
        return self._ledger.record_created(opportunity)

    def release(self, opportunity: CognitionOpportunity) -> None:
        self._ledger.release(opportunity.opportunity_id)

    def mark_honoured(self, opportunity: CognitionOpportunity) -> None:
        # Whether the turn answered the continuation is a fact about the plan,
        # not about the turn's outcome; `settle` reads it from both.
        pass

    def settle(self) -> tuple[str, ...]:
        """Give every finished, unanswered continuation its next state.

        Runs under the Core lock at the start of each tick, after plans have
        been reconciled. A continuation whose claim row records a finished
        turn while the plan still stands at that same identity was not
        answered. It is released when the provider was never reached, and
        retried under a new generation when it was. Returns what changed.
        """
        if not self._enabled:
            return ()
        settled = []
        now = self._clock()
        for goal_id in self._goals.list_open_plan_goal_ids():
            try:
                snapshot = self._goals.load(goal_id)
                plan = snapshot.state.execution_plan
                if plan is None or plan.status not in {"needs_core", "completed"}:
                    continue
                identifier = self._opportunity_id(goal_id, plan)
                outcome = self._ledger.outcome(identifier)
                # No claim, a turn still running, or an occasion held by the
                # input-bound mechanism, whose markers carry their bound.
                if outcome is None or outcome in _IN_PROGRESS or ":" in outcome:
                    continue
                if self._spend is not None and not self._spend.dispatch_started(identifier):
                    self._ledger.release(identifier)
                    self._not_before[identifier] = now + self._retry
                    settled.append(identifier)
                elif self._retry_generation(snapshot, identifier):
                    settled.append(identifier)
            except Exception as error:  # noqa: BLE001 - one goal must not stop the rest
                LOGGER.warning("Plan continuation could not be settled for goal %s: %s",
                               goal_id, type(error).__name__)
        return tuple(settled)

    def _retry_generation(self, snapshot, identifier: str) -> bool:
        """Offer an unanswered, possibly paid continuation once more, within the cap."""
        plan = snapshot.state.execution_plan
        if plan.continuation_generation >= MAX_CONTINUATION_GENERATION:
            if identifier not in self._exhausted_reported:
                self._exhausted_reported.add(identifier)
                LOGGER.warning(
                    "Plan continuation %s exhausted its retries; it waits for a turn "
                    "that selects goal %s", identifier, snapshot.state.goal_id)
            return False
        self._goals.replace(
            replace(snapshot.state, execution_plan=replace(
                plan, continuation_generation=plan.continuation_generation + 1)),
            snapshot.retention_until, snapshot.revision, snapshot.provenance,
        )
        return True

    def recover(self, spend=None) -> tuple[str, ...]:
        spend = self._spend if spend is None else spend
        return self._recover(spend)

    def _recover(self, spend) -> tuple[str, ...]:
        """Reclaim unfinished plan occasions at startup, one at a time.

        Runs while the runtime is composed. One stale or unreadable record
        must not stop AL/X starting, so each is isolated: a failure is
        logged and the occasion retained as unreconciled, never replayed.
        """
        reclaimed = []
        for row in self._ledger.unfinished():
            identifier = row["opportunity_id"]
            if not identifier.startswith("execution_plan:"):
                continue
            try:
                if spend is not None and spend.dispatch_started(identifier):
                    self._advance_recovery_generation(row)
                    self._ledger.mark_unreconciled(identifier)
                    continue
                self._ledger.release(identifier)
                reclaimed.append(identifier)
            except Exception as error:  # noqa: BLE001 - one record must not stop startup
                LOGGER.warning(
                    "Plan continuation recovery failed for opportunity %s (goal %s): %s",
                    identifier, self._goal_reference(row), type(error).__name__,
                )
                try:
                    self._ledger.mark_unreconciled(identifier)
                except Exception as mark_error:  # noqa: BLE001 - still keep starting
                    LOGGER.warning("Could not retain opportunity %s as unreconciled: %s",
                                   identifier, type(mark_error).__name__)
        return tuple(reclaimed)

    @staticmethod
    def _goal_reference(row) -> str | None:
        refs = str(row.get("refs") or "").split("\x1f")
        goal_ref = next((item for item in refs if item.startswith("execution_plan:")), None)
        return None if goal_ref is None else goal_ref[len("execution_plan:"):]

    def _advance_recovery_generation(self, row) -> None:
        refs = tuple(row.get("refs", "").split("\x1f"))
        goal_ref = next((item for item in refs if item.startswith("execution_plan:")), None)
        plan_ref = next((item for item in refs if item.startswith("execution_plan_id:")), None)
        cursor_ref = next((item for item in refs if item.startswith("execution_plan_cursor:")), None)
        generation_ref = next((item for item in refs
                               if item.startswith("execution_plan_generation:")), None)
        if None in (goal_ref, plan_ref, cursor_ref, generation_ref):
            return
        goal_id = goal_ref[len("execution_plan:"):]
        plan_id = plan_ref[len("execution_plan_id:"):]
        try:
            cursor = int(cursor_ref[len("execution_plan_cursor:"):])
            generation = int(generation_ref[len("execution_plan_generation:"):])
        except ValueError:
            return
        snapshot = self._goals.load(goal_id)
        plan = snapshot.state.execution_plan
        if (plan is None or plan.plan_id != plan_id or plan.cursor != cursor
                or plan.continuation_generation != generation
                or plan.status not in {"needs_core", "completed"}):
            return
        self._retry_generation(snapshot, row["opportunity_id"])
