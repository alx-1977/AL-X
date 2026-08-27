"""A configured authority boundary that does not interpret goals or capability meaning."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Mapping

from alx.contracts import Approval, CapabilityCall


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("evaluation time must be timezone-aware")


@dataclass(frozen=True, slots=True)
class AuthorityPolicy:
    permission_references: frozenset[str] = frozenset()
    approval_required: bool = False
    enabled: bool = True

    def __post_init__(self) -> None:
        references = frozenset(self.permission_references)
        if any(not isinstance(item, str) or not item.strip() for item in references):
            raise ValueError("permission references must be non-blank strings")
        object.__setattr__(self, "permission_references", references)


@dataclass(frozen=True, slots=True)
class AuthorityContext:
    principal_reference: str
    granted_permission_references: frozenset[str]
    evaluated_at: datetime
    approvals: tuple[Approval, ...] = ()

    def __post_init__(self) -> None:
        if not self.principal_reference.strip():
            raise ValueError("principal reference must not be blank")
        _aware(self.evaluated_at)
        references = frozenset(self.granted_permission_references)
        if any(not isinstance(item, str) or not item.strip() for item in references):
            raise ValueError("granted permission references must be non-blank strings")
        object.__setattr__(self, "granted_permission_references", references)
        object.__setattr__(self, "approvals", tuple(self.approvals))


class SafetyState(str, Enum):
    ALLOWED = "allowed"
    DENIED = "denied"
    APPROVAL_REQUIRED = "approval_required"


@dataclass(frozen=True, slots=True)
class SafetyOutcome:
    state: SafetyState
    reason: str

    @property
    def allowed(self) -> bool:
        return self.state is SafetyState.ALLOWED


class SafetyGate:
    """Injected policies authorize catalogue entries; side-effect metadata never grants authority."""
    def __init__(self, policies: Mapping[str, AuthorityPolicy]) -> None:
        self._policies = dict(policies)

    def evaluate(self, call: CapabilityCall, authority: AuthorityContext) -> SafetyOutcome:
        policy = self._policies.get(call.capability_id)
        if policy is None:
            return SafetyOutcome(SafetyState.DENIED, "policy_missing")
        if not policy.enabled:
            return SafetyOutcome(SafetyState.DENIED, "policy_denied")
        if not policy.permission_references <= authority.granted_permission_references:
            return SafetyOutcome(SafetyState.DENIED, "permission_missing")
        if not policy.approval_required:
            return SafetyOutcome(SafetyState.ALLOWED, "permitted")
        if call.approval_id is None:
            return SafetyOutcome(SafetyState.APPROVAL_REQUIRED, "approval_required")
        if any(item.approval_id == call.approval_id and item.permits(call, authority.evaluated_at) for item in authority.approvals):
            return SafetyOutcome(SafetyState.ALLOWED, "approved")
        return SafetyOutcome(SafetyState.DENIED, "approval_invalid")
