"""One transport-neutral entry into the authoritative AL/X Core."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from alx.contracts import ConversationTurn
from alx.core import CoreAgent, CoreOutcome


class ActiveGoalLocator(Protocol):
    """Locate durable continuation state without interpreting any words."""

    def __call__(self, conversation_id: str) -> str | None: ...


class ConversationGateway:
    def __init__(
        self,
        core: CoreAgent,
        locate_active_goal: ActiveGoalLocator | None = None,
        identifier_factory: Callable[[], str] | None = None,
    ) -> None:
        self._core = core
        self._locate_active_goal = locate_active_goal
        self._identifier_factory = identifier_factory or (lambda: str(uuid4()))

    def receive_conversation_turn(
        self,
        turn: ConversationTurn,
        step_budget: int,
        retention_until: datetime,
    ) -> CoreOutcome:
        """Continue durable Core state, or begin it, without reading the turn."""
        if self._locate_active_goal is None:
            raise RuntimeError("active goal locator is not configured")
        goal_id = self._locate_active_goal(turn.conversation_id)
        if goal_id is None:
            return self.receive(
                turn,
                self._identifier_factory(),
                step_budget,
                retention_until,
            )
        return self.receive(turn, goal_id, step_budget)

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
