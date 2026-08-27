"""One-call broker: validate, authorize, invoke, and return a structured outcome."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from alx.capabilities.registry import CapabilityRegistry, UnknownCapability
from alx.contracts import CapabilityCall, CapabilityResult, CapabilityResultState, StructuredData
from alx.safety import AuthorityContext, SafetyGate, SafetyOutcome


Executor = Callable[[StructuredData], CapabilityResult]


class BrokerState(str, Enum):
    EXECUTED = "executed"
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class BrokerOutcome:
    state: BrokerState
    safety: SafetyOutcome | None = None
    result: CapabilityResult | None = None
    reason: str | None = None


class CapabilityBroker:
    def __init__(self, registry: CapabilityRegistry, safety: SafetyGate, implementations: Mapping[str, Executor]) -> None:
        self._registry = registry
        self._safety = safety
        self._implementations = dict(implementations)

    def dispatch(self, call: CapabilityCall, authority: AuthorityContext) -> BrokerOutcome:
        try:
            definition = self._registry.lookup(call.capability_id)
        except UnknownCapability:
            return self._failed(call, "capability_unknown")
        if not definition.input_schema.accepts(call.arguments):
            return BrokerOutcome(BrokerState.REJECTED, reason="input_invalid")
        safety = self._safety.evaluate(call, authority)
        if not safety.allowed:
            return BrokerOutcome(BrokerState.REJECTED, safety=safety, reason=safety.reason)
        executor = self._implementations.get(call.capability_id)
        if executor is None:
            return self._failed(call, "implementation_missing", safety)
        try:
            result = executor(call.arguments)
        except Exception:
            return self._failed(call, "executor_error", safety)
        if not isinstance(result, CapabilityResult):
            return self._failed(call, "result_malformed", safety)
        if result.call_id != call.call_id or result.capability_id != call.capability_id:
            return self._failed(call, "result_identity_invalid", safety)
        if result.state is not CapabilityResultState.FAILED and not definition.output_schema.accepts(result.values):
            return self._failed(call, "result_output_invalid", safety)
        if result.state is CapabilityResultState.FAILED or (
            result.state is CapabilityResultState.PARTIAL and result.failure is not None
        ):
            failure = result.failure
            code = failure.get("code") if failure is not None else None
            if not isinstance(code, str) or not code.strip() or code not in definition.possible_failure_codes:
                return self._failed(call, "result_failure_invalid", safety)
        return BrokerOutcome(BrokerState.EXECUTED, safety=safety, result=result)

    @staticmethod
    def _failed(call: CapabilityCall, code: str, safety: SafetyOutcome | None = None) -> BrokerOutcome:
        result = CapabilityResult(call.call_id, call.capability_id, CapabilityResultState.FAILED, failure={"code": code})
        return BrokerOutcome(BrokerState.FAILED, safety=safety, result=result, reason=code)
