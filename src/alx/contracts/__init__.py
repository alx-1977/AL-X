"""Provider-neutral, immutable contracts shared by AL/X boundaries."""

from alx.contracts.records import (
    Approval,
    ApprovalProposal,
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
    GoalMutationKind,
    GoalProposal,
    GoalState,
    GoalStatus,
    GoalStopReason,
    Objective,
    ProgressRecord,
    Referent,
    SuccessCriterion,
    WorkItem,
)
from alx.contracts.provenance import (
    ContentOrigin,
    ContentProvenance,
    ContentTombstone,
    ExpiryReason,
    RetentionPolicy,
)
from alx.contracts.capabilities import CapabilityDefinition, SideEffect, StructuredSchema, ValueKind
from alx.contracts.core import AgentDecision, CapabilityDispatch, ConversationSnapshot, DecisionValidationError, DurableConversationStore, DurableGoalStore, DurableMemoryStore, GoalSnapshot, PendingMemoryBatch, ReasoningContext, ReasoningProvider
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
from alx.contracts.mail import (
    BackgroundEventSource,
    MailAccessError,
    MailAccount,
    MailAttachment,
    MailContent,
    MailObservationControl,
    MailParticipants,
    MailReference,
    MailSearchCriteria,
    MailSearchResult,
    MailSendError,
    MailThreading,
    OutboundReply,
    ReplyOutcome,
)
from alx.contracts.xero import XeroAccessError, XeroAccountingAccount
from alx.contracts.dhl import DhlDocumentError, DhlImportAnalyzer

__all__ = [
    "RetentionPolicy",
    "ExpiryReason",
    "ContentTombstone",
    "ContentProvenance",
    "ContentOrigin",
    "Approval", "ApprovalProposal", "ApprovalLifecycle", "ApprovalScope", "BackgroundEvent", "CapabilityCall", "CapabilityAttempt", "CapabilityAttemptDisposition",
    "CapabilityResult", "CapabilityResultState", "ConversationOrigin",
    "ConversationTurn", "Evidence", "GoalMutationKind", "GoalProposal", "GoalState", "GoalStatus", "GoalStopReason",
    "Objective", "ProgressRecord", "Referent", "SuccessCriterion", "WorkItem",
    "CapabilityDefinition", "SideEffect", "StructuredSchema", "ValueKind",
    "StructuredData",
    "AgentDecision", "CapabilityDispatch", "ConversationSnapshot", "DecisionValidationError", "DurableConversationStore", "DurableGoalStore", "DurableMemoryStore", "GoalSnapshot", "PendingMemoryBatch", "ReasoningContext", "ReasoningProvider",
    "AudioChunk", "SpeechSynthesizer", "SpeechTranscriber", "TranscriptionEvent",
    "TranscriptionState",
    "ModelCompletion", "ModelMessage", "ModelRequest", "ModelRole",
    "ReasoningModel",
    "MemoryCorrection", "MemoryKind", "MemoryProposal", "MemoryQuery",
    "MemoryRevision", "MemorySnapshot", "MemorySourceMatch",
    "BackgroundEventSource", "MailAccessError", "MailAccount", "MailAttachment", "MailContent",
    "MailObservationControl", "MailParticipants", "MailReference",
    "MailSearchCriteria", "MailSearchResult",
    "MailSendError", "MailThreading", "OutboundReply", "ReplyOutcome",
    "XeroAccessError", "XeroAccountingAccount",
    "DhlDocumentError", "DhlImportAnalyzer",
]
