"""One-call broker: validate, authorize, invoke, and return a structured outcome."""

from __future__ import annotations

from collections.abc import Callable
from typing import Mapping

from alx.capabilities.registry import CapabilityRegistry, UnknownCapability
from alx.contracts import CapabilityAttempt, CapabilityAttemptDisposition, CapabilityCall, CapabilityResult, CapabilityResultState, StructuredData
from alx.safety import AuthorityContext, SafetyGate, SafetyOutcome


Executor = Callable[[StructuredData], CapabilityResult]


class CapabilityBroker:
    def __init__(self, registry: CapabilityRegistry, safety: SafetyGate, implementations: Mapping[str, Executor]) -> None:
        self._registry = registry
        self._safety = safety
        self._implementations = dict(implementations)

    def dispatch(self, call: CapabilityCall, authority: AuthorityContext) -> CapabilityAttempt:
        try:
            definition = self._registry.lookup(call.capability_id)
        except UnknownCapability:
            return self._failed(call, "capability_unknown")
        if not definition.input_schema.accepts(call.arguments):
            return CapabilityAttempt(call, CapabilityAttemptDisposition.REJECTED, False, reason_code="input_invalid")
        safety = self._safety.evaluate(call, authority)
        if not safety.allowed:
            return CapabilityAttempt(call, CapabilityAttemptDisposition.REJECTED, False, reason_code=safety.reason)
        executor = self._implementations.get(call.capability_id)
        if executor is None:
            return self._failed(call, "implementation_missing", invoked=False)
        try:
            result = executor(call.arguments)
        except Exception:
            return self._failed(call, "executor_error", invoked=True)
        if not isinstance(result, CapabilityResult):
            return self._failed(call, "result_malformed", invoked=True)
        if result.call_id != call.call_id or result.capability_id != call.capability_id:
            return self._failed(call, "result_identity_invalid", invoked=True)
        if result.state is not CapabilityResultState.FAILED and not definition.output_schema.accepts(result.values):
            return self._failed(call, "result_output_invalid", invoked=True)
        if result.state is CapabilityResultState.FAILED or (
            result.state is CapabilityResultState.PARTIAL and result.failure is not None
        ):
            failure = result.failure
            code = failure.get("code") if failure is not None else None
            if not isinstance(code, str) or not code.strip() or code not in definition.possible_failure_codes:
                return self._failed(call, "result_failure_invalid", invoked=True)
        return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True, result)

    @staticmethod
    def _failed(call: CapabilityCall, code: str, invoked: bool = False) -> CapabilityAttempt:
        result = CapabilityResult(call.call_id, call.capability_id, CapabilityResultState.FAILED, failure={"code": code})
        return CapabilityAttempt(call, CapabilityAttemptDisposition.BROKER_FAILURE, invoked, result, code)
