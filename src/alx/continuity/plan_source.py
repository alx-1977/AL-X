"""Offer a completed or interrupted execution plan to the existing Core runner."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from alx.contracts.cognition import CognitionOrigin
from alx.contracts.continuity import CognitionOpportunity


class PlanContinuationSource:
    def __init__(self, goals, ledger, enabled: bool = False) -> None:
        self._goals = goals
        self._ledger = ledger
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    def due_opportunities(self) -> tuple[CognitionOpportunity, ...]:
        if not self._enabled:
            return ()
        result = []
        now = datetime.now(UTC)
        for snapshot in self._goals.list_goals():
            if snapshot.retention_until <= now:
                continue
            plan = snapshot.state.execution_plan
            if (plan is None or plan.status not in {"needs_core", "completed"}
                    or plan.response_turn_id is not None):
                # A response already being stored is reconciled by the Core's
                # due tick, never offered as a second occasion.
                continue
            identifier = self._opportunity_id(snapshot.state.goal_id, plan)
            if self._ledger.exists(identifier):
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
        pass

    def recover(self, spend=None) -> tuple[str, ...]:
        reclaimed = []
        for row in self._ledger.unfinished():
            identifier = row["opportunity_id"]
            if not identifier.startswith("execution_plan:"):
                continue
            if spend is not None and spend.dispatch_started(identifier):
                self._advance_recovery_generation(row)
                self._ledger.mark_unreconciled(identifier)
                continue
            self._ledger.release(identifier)
            reclaimed.append(identifier)
        return tuple(reclaimed)

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
        updated = replace(plan, continuation_generation=generation + 1)
        self._goals.replace(
            replace(snapshot.state, execution_plan=updated),
            snapshot.retention_until, snapshot.revision, snapshot.provenance,
        )
