"""Provider-neutral ports and decisions for the sole AL/X reasoning loop."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol

from alx.contracts.capabilities import CapabilityDefinition
from alx.contracts.cognition import CognitionOrigin
from alx.contracts.continuity import CarriedThought
from alx.contracts.records import (
    ApprovalProposal,
    BackgroundEvent,
    CapabilityAttempt,
    CapabilityAttemptDisposition,
    CapabilityCall,
    ConversationTurn,
    GoalMutationKind,
    GoalProposal,
    GoalState,
    GoalStatus,
    GoalStopReason,
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
class GoalSummary:
    """Enough to recognise an unfinished goal, never enough to act on it.

    A conversation may carry several unfinished goals at once. The Core sees
    every one of them in this compact form and decides which, if any, the
    current input belongs to. Full state is loaded only for the goal it
    selects, so a long history is never paid for on every reasoning call.
    """

    goal_id: str
    objective_summary: str
    status: GoalStatus
    stop_reason: GoalStopReason | None = None
    outstanding_work: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    has_pending_dispatch: bool = False

    def __post_init__(self) -> None:
        if not self.goal_id.strip():
            raise ValueError("goal_id must not be blank")
        if not isinstance(self.status, GoalStatus):
            raise TypeError("status must be a GoalStatus")
        object.__setattr__(self, "outstanding_work", tuple(self.outstanding_work))
        object.__setattr__(self, "blockers", tuple(self.blockers))

    @classmethod
    def of(cls, state: GoalState) -> GoalSummary:
        return cls(
            state.goal_id,
            state.objective.summary,
            state.status,
            state.stop_reason,
            tuple(item.summary for item in state.outstanding_work),
            tuple(item.summary for item in state.blockers),
            any(
                item.disposition is CapabilityAttemptDisposition.PENDING
                for item in state.attempts
            ),
        )

    @property
    def unfinished(self) -> bool:
        return self.status not in (GoalStatus.COMPLETED, GoalStatus.CANCELLED)


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
    # Every unfinished goal of the conversation, compactly. `active_goal` is
    # the full state of the one the Core has selected this turn, if any.
    unfinished_goals: tuple[GoalSummary, ...] = ()
    # Where this turn came from. Provenance only: it says nobody asked, or that
    # Friedl did. Nothing may be derived from it about what to think about.
    origin: CognitionOrigin = CognitionOrigin.PERSON_TURN
    # Thoughts AL/X still holds, most recent first and bounded by count. Every
    # turn gets the same list by the same rule, because a context assembled
    # differently for an unprompted turn would be a second builder deciding
    # what she is like when nobody is watching.
    carried_thoughts: tuple[CarriedThought, ...] = ()
    # Autonomous occasions whose response never reached anyone. References and
    # timing only: the words are not kept, so she is told that it happened and
    # decides afresh whether anything still needs saying.
    undelivered_responses: tuple[Mapping[str, Any], ...] = ()
    # Memory identifiers she proposed this turn that already name a different
    # memory. Mechanical facts only -- the identifier, and the content already
    # stored under it -- because whether to supersede it, choose another
    # identifier, or let the write go is hers to judge under Law 3.
    memory_conflicts: tuple[Mapping[str, Any], ...] = ()
    # Calls refused this turn before any approval or dispatch, with the
    # mechanical reason. She is told once so she can explain or correct;
    # repeating the same refused call ends the turn rather than buying
    # another reasoning step against an unchanged state.
    refused_calls: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "turns", tuple(self.turns))
        object.__setattr__(self, "unfinished_goals", tuple(self.unfinished_goals))
        object.__setattr__(self, "carried_thoughts", tuple(self.carried_thoughts))
        object.__setattr__(self, "memory_conflicts", tuple(self.memory_conflicts))
        object.__setattr__(self, "refused_calls", tuple(self.refused_calls))
        object.__setattr__(
            self, "undelivered_responses", tuple(self.undelivered_responses)
        )
        if self.active_goal is not None and not any(
            item.goal_id == self.active_goal.goal_id for item in self.unfinished_goals
        ):
            raise ValueError("the active goal must be one of the unfinished goals")
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
    finish_silently: bool = False
    goal_proposal: GoalProposal | None = None
    response_requires_goal_commit: bool = False
    memory_proposals: tuple[MemoryProposal, ...] = ()
    memory_query: MemoryQuery | None = None
    approval_proposal: ApprovalProposal | None = None
    # The unfinished goal this decision works under, chosen by the Core from
    # the summaries it was shown. None with a create proposal starts a new
    # goal; None without one is goal-less conversation. A decision that
    # carries only a goal_id asks for that goal's full state before acting.
    goal_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.finish_silently, bool):
            raise TypeError("finish_silently must be a bool")
        chosen = sum(item is not None for item in (self.call, self.response, self.memory_query))
        chosen += int(self.finish_silently)
        if chosen == 0 and self.goal_id is not None:
            # Selecting a goal may carry a mutation of that same goal: the
            # Core says which work this is and what it now knows about it in
            # one decision. An approval may not travel alone, because an
            # approval authorises one exact call and there is none here.
            if self.approval_proposal is not None or self.response_requires_goal_commit:
                raise ValueError("a goal selection carries no approval or commit flag")
        elif chosen != 1:
            raise ValueError("a decision contains exactly one call, response, or memory query")
        if self.goal_id is not None and not self.goal_id.strip():
            raise ValueError("goal_id must not be blank")
        if (
            self.goal_id is not None
            and self.goal_proposal is not None
            and self.goal_proposal.kind is GoalMutationKind.CREATE
        ):
            raise ValueError("a decision cannot both select a goal and create one")
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

    @property
    def selects_only(self) -> bool:
        return (
            self.goal_id is not None
            and self.call is None
            and self.response is None
            and self.memory_query is None
            and not self.finish_silently
        )


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

    def list_unfinished(self, conversation_id: str) -> tuple[GoalSummary, ...]: ...

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

    def load(self, memory_id: str) -> MemorySnapshot: ...


class CapabilityDispatch(Protocol):
    def __call__(self, call: CapabilityCall, state: GoalState | None) -> CapabilityAttempt: ...
