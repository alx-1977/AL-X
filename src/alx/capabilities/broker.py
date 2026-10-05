"""One-call broker: validate, authorize, invoke, and return a structured outcome."""

from __future__ import annotations

import re
from collections.abc import Callable
from time import monotonic
from typing import Any, Mapping

from alx.capabilities.registry import CapabilityRegistry, UnknownCapability
from alx.contracts import (
    CapabilityAttempt, CapabilityAttemptDisposition, CapabilityCall, CapabilityDefinition,
    CapabilityResult, CapabilityResultState, StructuredData, TraceSink,
    TraceStatus, TraceSubsystem, emit_trace,
)
from alx.safety import AuthorityContext, SafetyGate, SafetyOutcome


Executor = Callable[[StructuredData], CapabilityResult]

# The shape a declared trace field's value must have to be shown: a short
# identifier token. Anything longer or freer is omitted, so a capability that
# declares a field cannot leak wording through it by mistake.
_TRACE_TOKEN = re.compile(r"[A-Za-z0-9_.:/#-]{1,48}")


class CapabilityBroker:
    def __init__(
        self,
        registry: CapabilityRegistry,
        safety: SafetyGate,
        implementations: Mapping[str, Executor],
        trace: TraceSink | None = None,
        subsystems: Mapping[str, TraceSubsystem] | None = None,
    ) -> None:
        self._registry = registry
        self._safety = safety
        self._implementations = dict(implementations)
        # Every dispatch passes through here, turn or planned step alike, so
        # this is where the operator sees which capability is running.
        self._trace = trace
        # Which subsystem each capability belongs to, as composition
        # registered it. A capability composition did not name is CAPABILITY.
        self._subsystems = dict(subsystems or {})

    def dispatch(self, call: CapabilityCall, authority: AuthorityContext) -> CapabilityAttempt:
        try:
            definition = self._registry.lookup(call.capability_id)
        except UnknownCapability:
            return self._failed(call, "capability_unknown")
        if not definition.input_schema.accepts(call.arguments):
            self._emit(definition, call, TraceStatus.REFUSED, reason_code="input_invalid")
            return CapabilityAttempt(call, CapabilityAttemptDisposition.REJECTED, False, reason_code="input_invalid")
        safety = self._safety.evaluate(call, authority)
        if not safety.allowed:
            self._emit(definition, call, TraceStatus.REFUSED, reason_code=safety.reason)
            return CapabilityAttempt(call, CapabilityAttemptDisposition.REJECTED, False, reason_code=safety.reason)
        executor = self._implementations.get(call.capability_id)
        if executor is None:
            return self._failed(call, "implementation_missing", invoked=False)
        self._emit(definition, call, TraceStatus.STARTED)
        started_at = monotonic()
        attempt = self._invoke(definition, call, executor)
        result = attempt.result
        failure = None if result is None or result.failure is None else result.failure.get("code")
        self._emit(
            definition, call,
            TraceStatus.COMPLETED if result is not None
            and result.state is not CapabilityResultState.FAILED else TraceStatus.FAILED,
            reason_code=(
                attempt.reason_code or (failure if isinstance(failure, str) else None)
                or ("partial" if result is not None
                    and result.state is CapabilityResultState.PARTIAL else None)
            ),
            duration_ms=round((monotonic() - started_at) * 1000),
        )
        return attempt

    def _invoke(self, definition: CapabilityDefinition, call: CapabilityCall,
                executor: Executor) -> CapabilityAttempt:
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

    def _emit(self, definition: CapabilityDefinition, call: CapabilityCall,
              status: TraceStatus, **values: Any) -> None:
        if self._trace is None:
            return
        shown = [
            f"#{value}" if isinstance(value, int) else value
            for value in (call.arguments.get(name) for name in definition.trace_fields)
            if (isinstance(value, int) and not isinstance(value, bool) and value >= 0)
            or (isinstance(value, str) and _TRACE_TOKEN.fullmatch(value))
        ]
        emit_trace(
            self._trace,
            self._subsystems.get(call.capability_id, TraceSubsystem.CAPABILITY),
            status,
            call.capability_id.replace("_", " ").capitalize(),
            # The label is the capability; a reference only adds declared
            # identifiers. Repeating the id beside its own label was clutter.
            reference=" ".join(str(item) for item in shown)[:96] or None,
            **values,
        )

    @staticmethod
    def _failed(call: CapabilityCall, code: str, invoked: bool = False) -> CapabilityAttempt:
        result = CapabilityResult(call.call_id, call.capability_id, CapabilityResultState.FAILED, failure={"code": code})
        return CapabilityAttempt(call, CapabilityAttemptDisposition.BROKER_FAILURE, invoked, result, code)
