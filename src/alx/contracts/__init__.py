"""Provider-neutral, immutable contracts shared by AL/X boundaries."""

from alx.contracts.records import (
    Approval,
    ApprovalLifecycle,
    ApprovalScope,
    BackgroundEvent,
    CapabilityCall,
    CapabilityAttempt,
    CapabilityAttemptDisposition,
    CapabilityResult,
    CapabilityResultState,
    ConversationOrigin,
    ConversationTurn,
    Evidence,
    GoalState,
    GoalStatus,
    GoalStopReason,
    Objective,
    ProgressRecord,
    Referent,
    SuccessCriterion,
    WorkItem,
)
from alx.contracts.capabilities import CapabilityDefinition, SideEffect, StructuredSchema, ValueKind
from alx.contracts.core import AgentDecision, CapabilityDispatch, DurableGoalStore, DurableMemoryStore, GoalSnapshot, PendingMemoryBatch, ReasoningContext, ReasoningProvider
from alx.contracts.records import StructuredData
from alx.contracts.speech import (
    AudioChunk,
    SpeechSynthesizer,
    SpeechTranscriber,
    TranscriptionEvent,
    TranscriptionState,
)
from alx.contracts.models import (
    ModelCompletion,
    ModelMessage,
    ModelRequest,
    ModelRole,
    ReasoningModel,
)
from alx.contracts.memory import (
    MemoryCorrection,
    MemoryKind,
    MemoryProposal,
    MemoryQuery,
    MemoryRevision,
    MemorySnapshot,
    MemorySourceMatch,
)

__all__ = [
    "Approval", "ApprovalLifecycle", "ApprovalScope", "BackgroundEvent", "CapabilityCall", "CapabilityAttempt", "CapabilityAttemptDisposition",
    "CapabilityResult", "CapabilityResultState", "ConversationOrigin",
    "ConversationTurn", "Evidence", "GoalState", "GoalStatus", "GoalStopReason",
    "Objective", "ProgressRecord", "Referent", "SuccessCriterion", "WorkItem",
    "CapabilityDefinition", "SideEffect", "StructuredSchema", "ValueKind",
    "StructuredData",
    "AgentDecision", "CapabilityDispatch", "DurableGoalStore", "DurableMemoryStore", "GoalSnapshot", "PendingMemoryBatch", "ReasoningContext", "ReasoningProvider",
    "AudioChunk", "SpeechSynthesizer", "SpeechTranscriber", "TranscriptionEvent",
    "TranscriptionState",
    "ModelCompletion", "ModelMessage", "ModelRequest", "ModelRole",
    "ReasoningModel",
    "MemoryCorrection", "MemoryKind", "MemoryProposal", "MemoryQuery",
    "MemoryRevision", "MemorySnapshot", "MemorySourceMatch",
]
