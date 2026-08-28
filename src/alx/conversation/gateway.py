"""One transport-neutral entry into the authoritative AL/X Core."""

from __future__ import annotations

from datetime import datetime

from alx.contracts import ConversationTurn
from alx.core import CoreAgent, CoreOutcome


class ConversationGateway:
    def __init__(self, core: CoreAgent) -> None:
        self._core = core

    def receive(
        self,
        turn: ConversationTurn,
        goal_id: str,
        step_budget: int,
        retention_until: datetime | None = None,
    ) -> CoreOutcome:
        """Transport a turn without inspecting its wording or origin."""
        if retention_until is None:
            return self._core.continue_goal(goal_id, turn, step_budget)
        return self._core.start(goal_id, turn, retention_until, step_budget)
