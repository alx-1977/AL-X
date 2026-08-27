"""Provider-neutral ports and decisions for the sole AL/X reasoning loop."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from alx.contracts.capabilities import CapabilityDefinition
from alx.contracts.records import (
    CapabilityAttempt,
    CapabilityCall,
    ConversationTurn,
    GoalState,
)


@dataclass(frozen=True, slots=True)
class GoalSnapshot:
    state: GoalState
    turns: tuple[ConversationTurn, ...]
    revision: int
    retention_until: datetime

    def __post_init__(self) -> None:
        if self.revision <= 0:
            raise ValueError("revision must be positive")
        if (
            self.retention_until.tzinfo is None
            or self.retention_until.utcoffset() is None
        ):
            raise ValueError("retention_until must be timezone-aware")
        object.__setattr__(self, "turns", tuple(self.turns))


@dataclass(frozen=True, slots=True)
class ReasoningContext:
    goal: GoalState
    turns: tuple[ConversationTurn, ...]
    capabilities: tuple[CapabilityDefinition, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "turns", tuple(self.turns))
        object.__setattr__(self, "capabilities", tuple(self.capabilities))


@dataclass(frozen=True, slots=True)
class AgentDecision:
    goal: GoalState
    call: CapabilityCall | None = None
    response: str | None = None

    def __post_init__(self) -> None:
        if (self.call is None) == (self.response is None):
            raise ValueError("a decision contains exactly one call or response")
        if self.response is not None and not self.response.strip():
            raise ValueError("response must not be blank")


class ReasoningProvider(Protocol):
    def decide(self, context: ReasoningContext) -> AgentDecision: ...


class DurableGoalStore(Protocol):
    def load(self, goal_id: str) -> GoalSnapshot: ...

    def replace(
        self,
        state: GoalState,
        turns: tuple[ConversationTurn, ...],
        retention_until: datetime,
        expected_revision: int,
    ) -> GoalSnapshot: ...


class CapabilityDispatch(Protocol):
    def __call__(self, call: CapabilityCall, state: GoalState) -> CapabilityAttempt: ...
