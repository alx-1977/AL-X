"""Provider-neutral ports and decisions for the sole AL/X reasoning loop."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from alx.contracts.capabilities import CapabilityDefinition
from alx.contracts.records import (
    ApprovalProposal,
    BackgroundEvent,
    CapabilityAttempt,
    CapabilityCall,
    ConversationTurn,
    GoalProposal,
    GoalState,
)
from alx.contracts.memory import MemoryProposal, MemoryQuery, MemorySnapshot
from alx.contracts.provenance import ContentProvenance


class DecisionValidationError(ValueError):
    """The model returned a decision that cannot safely enter durable state."""

    def __init__(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("decision validation reason must not be blank")
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class GoalSnapshot:
    state: GoalState
    conversation_id: str
    revision: int
    retention_until: datetime
    provenance: ContentProvenance | None = None

    def __post_init__(self) -> None:
        if self.revision <= 0:
            raise ValueError("revision must be positive")
        if (
            self.retention_until.tzinfo is None
            or self.retention_until.utcoffset() is None
        ):
            raise ValueError("retention_until must be timezone-aware")
        if not self.conversation_id.strip():
            raise ValueError("conversation_id must not be blank")


@dataclass(frozen=True, slots=True)
class ConversationSnapshot:
    conversation_id: str
    turns: tuple[ConversationTurn, ...]
    revision: int
    retention_until: datetime
    events: tuple[BackgroundEvent, ...] = ()

    def __post_init__(self) -> None:
        if not self.conversation_id.strip():
            raise ValueError("conversation_id must not be blank")
        if self.revision <= 0:
            raise ValueError("revision must be positive")
        if self.retention_until.tzinfo is None or self.retention_until.utcoffset() is None:
            raise ValueError("retention_until must be timezone-aware")
        object.__setattr__(self, "turns", tuple(self.turns))
        object.__setattr__(self, "events", tuple(self.events))
        if any(turn.conversation_id != self.conversation_id for turn in self.turns):
            raise ValueError("all turns must belong to the snapshot conversation")


@dataclass(frozen=True, slots=True)
class PendingMemoryBatch:
    """Exact Core memory proposals durably coupled to one goal revision."""

    goal_id: str
    goal_revision: int
    proposals: tuple[MemoryProposal, ...]
    retention_until: datetime

    def __post_init__(self) -> None:
        if not self.goal_id.strip():
            raise ValueError("goal_id must not be blank")
        if self.goal_revision <= 0:
            raise ValueError("goal_revision must be positive")
        if not self.proposals:
            raise ValueError("a pending memory batch must contain proposals")
        memory_ids = [proposal.memory_id for proposal in self.proposals]
        if len(memory_ids) != len(set(memory_ids)):
            raise ValueError("a pending memory batch cannot repeat a memory identifier")
        if (
            self.retention_until.tzinfo is None
            or self.retention_until.utcoffset() is None
        ):
            raise ValueError("retention_until must be timezone-aware")
        object.__setattr__(self, "proposals", tuple(self.proposals))


@dataclass(frozen=True, slots=True)
class ReasoningContext:
    active_goal: GoalState | None
    turns: tuple[ConversationTurn, ...]
    capabilities: tuple[CapabilityDefinition, ...]
    memories: tuple[MemorySnapshot, ...] = ()
    events: tuple[BackgroundEvent, ...] = ()
    transient_attempts: tuple[CapabilityAttempt, ...] = ()
    conversation_id: str | None = None
    trigger_event_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "turns", tuple(self.turns))
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        object.__setattr__(self, "memories", tuple(self.memories))
        object.__setattr__(self, "events", tuple(self.events))
        object.__setattr__(self, "transient_attempts", tuple(self.transient_attempts))
        conversation_id = self.conversation_id
        if conversation_id is None and self.turns:
            conversation_id = self.turns[-1].conversation_id
        if conversation_id is None or not conversation_id.strip():
            raise ValueError("reasoning context requires a conversation identifier")
        object.__setattr__(self, "conversation_id", conversation_id)
        if self.trigger_event_id is not None and not any(
            item.event_id == self.trigger_event_id for item in self.events
        ):
            raise ValueError("trigger event must be present in reasoning context")

@dataclass(frozen=True, slots=True)
class AgentDecision:
    call: CapabilityCall | None = None
    response: str | None = None
    goal_proposal: GoalProposal | None = None
    response_requires_goal_commit: bool = False
    memory_proposals: tuple[MemoryProposal, ...] = ()
    memory_query: MemoryQuery | None = None
    approval_proposal: ApprovalProposal | None = None

    def __post_init__(self) -> None:
        if sum(item is not None for item in (self.call, self.response, self.memory_query)) != 1:
            raise ValueError("a decision contains exactly one call, response, or memory query")
        if self.response is not None and not self.response.strip():
            raise ValueError("response must not be blank")
        if self.response_requires_goal_commit and self.response is None:
            raise ValueError("only a response can depend on a goal commit")
        if self.approval_proposal is not None and self.call is None:
            raise ValueError("an approval proposal requires an exact capability call")
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
        conversation_id: str,
        retention_until: datetime,
        provenance: ContentProvenance | None = None,
    ) -> GoalSnapshot: ...

    def load(self, goal_id: str) -> GoalSnapshot: ...

    def replace(
        self,
        state: GoalState,
        retention_until: datetime,
        expected_revision: int,
        provenance: ContentProvenance | None = None,
    ) -> GoalSnapshot: ...

    def replace_with_memory_batch(
        self,
        state: GoalState,
        retention_until: datetime,
        expected_revision: int,
        proposals: tuple[MemoryProposal, ...],
        provenance: ContentProvenance | None = None,
    ) -> GoalSnapshot: ...

    def pending_memory_batches(
        self,
        goal_id: str,
    ) -> tuple[PendingMemoryBatch, ...]: ...

    def acknowledge_memory_batch(
        self,
        goal_id: str,
        goal_revision: int,
    ) -> None: ...


class DurableConversationStore(Protocol):
    def create(
        self,
        conversation_id: str,
        retention_until: datetime,
    ) -> ConversationSnapshot: ...

    def load(self, conversation_id: str) -> ConversationSnapshot: ...

    def append(
        self,
        turn: ConversationTurn,
        retention_until: datetime,
        expected_revision: int,
    ) -> ConversationSnapshot: ...

class DurableMemoryStore(Protocol):
    def remember(
        self,
        proposal: MemoryProposal,
        retention_until: datetime,
    ) -> MemorySnapshot: ...

    def remember_many(
        self,
        proposals: tuple[MemoryProposal, ...],
        retention_until: datetime,
    ) -> tuple[MemorySnapshot, ...]: ...

    def retrieve(
        self,
        query: MemoryQuery,
        as_of: datetime,
    ) -> tuple[MemorySnapshot, ...]: ...


class CapabilityDispatch(Protocol):
    def __call__(self, call: CapabilityCall, state: GoalState | None) -> CapabilityAttempt: ...
