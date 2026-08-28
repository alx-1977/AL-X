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
from alx.contracts.memory import MemoryProposal, MemoryQuery, MemorySnapshot


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
    memories: tuple[MemorySnapshot, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "turns", tuple(self.turns))
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        object.__setattr__(self, "memories", tuple(self.memories))


@dataclass(frozen=True, slots=True)
class AgentDecision:
    goal: GoalState
    call: CapabilityCall | None = None
    response: str | None = None
    memory_proposals: tuple[MemoryProposal, ...] = ()
    memory_query: MemoryQuery | None = None

    def __post_init__(self) -> None:
        if sum(item is not None for item in (self.call, self.response, self.memory_query)) != 1:
            raise ValueError("a decision contains exactly one call, response, or memory query")
        if self.response is not None and not self.response.strip():
            raise ValueError("response must not be blank")
        object.__setattr__(self, "memory_proposals", tuple(self.memory_proposals))
        memory_ids = [item.memory_id for item in self.memory_proposals]
        if len(memory_ids) != len(set(memory_ids)):
            raise ValueError("a decision cannot repeat a memory identifier")


class ReasoningProvider(Protocol):
    def decide(self, context: ReasoningContext) -> AgentDecision: ...


class DurableGoalStore(Protocol):
    def create(
        self,
        state: GoalState,
        turns: tuple[ConversationTurn, ...],
        retention_until: datetime,
    ) -> GoalSnapshot: ...

    def load(self, goal_id: str) -> GoalSnapshot: ...

    def replace(
        self,
        state: GoalState,
        turns: tuple[ConversationTurn, ...],
        retention_until: datetime,
        expected_revision: int,
    ) -> GoalSnapshot: ...


class DurableMemoryStore(Protocol):
    def remember(
        self,
        proposal: MemoryProposal,
        retention_until: datetime,
    ) -> MemorySnapshot: ...

    def retrieve(
        self,
        query: MemoryQuery,
        as_of: datetime,
    ) -> tuple[MemorySnapshot, ...]: ...


class CapabilityDispatch(Protocol):
    def __call__(self, call: CapabilityCall, state: GoalState) -> CapabilityAttempt: ...
