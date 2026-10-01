"""Offer a completed or interrupted execution plan to the existing Core runner."""

from __future__ import annotations

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
        for summary in self._goals.list_unfinished():
            snapshot = self._goals.load(summary.goal_id)
            plan = snapshot.state.execution_plan
            if plan is None or plan.status not in {"needs_core", "completed"}:
                continue
            identifier = f"execution_plan:{summary.goal_id}:{plan.plan_id}:{plan.cursor}"
            if self._ledger.exists(identifier):
                continue
            result.append(CognitionOpportunity(
                identifier, CognitionOrigin.WORK_COMPLETED,
                summary.updated_at or datetime.now(UTC), snapshot.conversation_id,
                references=(f"execution_plan:{summary.goal_id}",),
            ))
        return tuple(result)

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
                self._ledger.mark_unreconciled(identifier)
                continue
            self._ledger.release(identifier)
            reclaimed.append(identifier)
        return tuple(reclaimed)
