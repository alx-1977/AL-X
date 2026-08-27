from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.capabilities import CapabilityBroker, CapabilityRegistry, DuplicateCapability  # noqa: E402
from alx.contracts import (  # noqa: E402
    Approval, ApprovalLifecycle, ApprovalScope, CapabilityAttemptDisposition,
    CapabilityCall, CapabilityDefinition, CapabilityResult, CapabilityResultState,
    SideEffect, StructuredSchema, ValueKind,
)
from alx.safety import AuthorityContext, AuthorityPolicy, SafetyGate, SafetyState  # noqa: E402

NOW = datetime(2026, 8, 27, tzinfo=UTC)
INPUT = StructuredSchema(ValueKind.OBJECT, {"value": StructuredSchema(ValueKind.INTEGER), "items": StructuredSchema(ValueKind.ARRAY, items=StructuredSchema(ValueKind.STRING))}, ("value",), extra_properties=False)
OUTPUT = StructuredSchema(ValueKind.OBJECT, {"ok": StructuredSchema(ValueKind.BOOLEAN)}, ("ok",), extra_properties=False)

def definition(identifier: str = "capability-1") -> CapabilityDefinition:
    return CapabilityDefinition(identifier, "describes one reusable external ability", INPUT, OUTPUT, SideEffect.NONE, ("unavailable",))

def authority(*, permissions: frozenset[str] = frozenset({"permission-1"}), approvals: tuple[Approval, ...] = ()) -> AuthorityContext:
    return AuthorityContext("principal-1", permissions, NOW, approvals)

class SchemaTests(unittest.TestCase):
    def test_nested_schema_and_bool_number_edges(self) -> None:
        self.assertTrue(INPUT.accepts({"value": 1, "items": ["a"]}))
        self.assertFalse(INPUT.accepts({"value": True}))
        self.assertFalse(StructuredSchema(ValueKind.NUMBER).accepts(True))
        self.assertFalse(INPUT.accepts({"items": []}))
        self.assertFalse(INPUT.accepts({"value": 1, "extra": 2}))
        self.assertFalse(INPUT.accepts({"value": 1, "items": [2]}))
        self.assertFalse(StructuredSchema(ValueKind.ARRAY).accepts([object()]))
        self.assertFalse(StructuredSchema(ValueKind.OBJECT).accepts({"undeclared": object()}))
        with self.assertRaises(TypeError):
            StructuredSchema("object")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            StructuredSchema(ValueKind.ARRAY, items=object())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            StructuredSchema(ValueKind.OBJECT, extra_properties=1)  # type: ignore[arg-type]

class RegistryAndBrokerTests(unittest.TestCase):
    def test_registry_is_descriptive_and_rejects_duplicates(self) -> None:
        registry = CapabilityRegistry((definition(),))
        self.assertEqual(registry.list_definitions(), (definition(),))
        self.assertNotIn("triggers", definition().__dataclass_fields__)
        self.assertNotIn("order", definition().__dataclass_fields__)
        with self.assertRaises(DuplicateCapability):
            registry.register(definition())
        with self.assertRaises(ValueError):
            CapabilityDefinition("scalar", "purpose", StructuredSchema(ValueKind.STRING), OUTPUT, SideEffect.NONE)
        with self.assertRaises(TypeError):
            CapabilityDefinition("effect", "purpose", INPUT, OUTPUT, "none")  # type: ignore[arg-type]

    def test_policy_is_configured_and_missing_fails_closed(self) -> None:
        call = CapabilityCall("call-1", "capability-1", {"value": 1})
        self.assertEqual(SafetyGate({}).evaluate(call, authority()).state, SafetyState.DENIED)
        policy = AuthorityPolicy(frozenset({"permission-1"}))
        self.assertEqual(SafetyGate({"capability-1": policy}).evaluate(call, authority()).state, SafetyState.ALLOWED)
        self.assertEqual(SafetyGate({"capability-1": policy}).evaluate(call, authority(permissions=frozenset())).state, SafetyState.DENIED)

    def test_rejections_never_execute_and_exact_approval_allows(self) -> None:
        calls = [0]
        def execute(data: object) -> CapabilityResult:
            calls[0] += 1
            return CapabilityResult("call-1", "capability-1", CapabilityResultState.SUCCEEDED, {"ok": True})
        registry = CapabilityRegistry((definition(),))
        gate = SafetyGate({"capability-1": AuthorityPolicy(frozenset({"permission-1"}), approval_required=True)})
        broker = CapabilityBroker(registry, gate, {"capability-1": execute})  # type: ignore[arg-type]
        blocked = broker.dispatch(CapabilityCall("call-1", "capability-1", {"value": 1}), authority())
        self.assertEqual(blocked.state.value, "rejected")
        self.assertEqual(calls[0], 0)
        approval = Approval("approval-1", ApprovalScope("capability-1", {"value": 1}), ApprovalLifecycle.GRANTED, NOW + timedelta(minutes=1))
        outcome = broker.dispatch(CapabilityCall("call-1", "capability-1", {"value": 1}, "approval-1"), authority(approvals=(approval,)))
        self.assertEqual(outcome.result, CapabilityResult("call-1", "capability-1", CapabilityResultState.SUCCEEDED, {"ok": True}))
        self.assertEqual(calls[0], 1)

    def test_denied_wrong_scope_and_expired_approvals_never_execute(self) -> None:
        calls = [0]
        def execute(data: object) -> CapabilityResult:
            calls[0] += 1
            return CapabilityResult("call-1", "capability-1", CapabilityResultState.SUCCEEDED, {"ok": True})
        registry = CapabilityRegistry((definition(),))
        call = CapabilityCall("call-1", "capability-1", {"value": 1}, "approval-1")
        denied = CapabilityBroker(registry, SafetyGate({"capability-1": AuthorityPolicy(enabled=False)}), {"capability-1": execute})  # type: ignore[arg-type]
        self.assertEqual(denied.dispatch(call, authority()).state.value, "rejected")
        required = AuthorityPolicy(approval_required=True)
        broker = CapabilityBroker(registry, SafetyGate({"capability-1": required}), {"capability-1": execute})  # type: ignore[arg-type]
        wrong = Approval("approval-1", ApprovalScope("capability-1", {"value": 2}), ApprovalLifecycle.GRANTED, NOW + timedelta(minutes=1))
        expired = Approval("approval-1", ApprovalScope("capability-1", {"value": 1}), ApprovalLifecycle.GRANTED, NOW - timedelta(minutes=1))
        self.assertEqual(broker.dispatch(call, authority(approvals=(wrong,))).state.value, "rejected")
        self.assertEqual(broker.dispatch(call, authority(approvals=(expired,))).state.value, "rejected")
        consumed = Approval("approval-1", ApprovalScope("capability-1", {"value": 1}), ApprovalLifecycle.CONSUMED, NOW + timedelta(minutes=1))
        self.assertEqual(broker.dispatch(call, authority(approvals=(consumed,))).state.value, "rejected")
        self.assertEqual(calls[0], 0)

    def test_missing_permission_never_invokes_executor(self) -> None:
        calls = [0]
        def execute(data: object) -> CapabilityResult:
            calls[0] += 1
            return CapabilityResult("call-1", "capability-1", CapabilityResultState.SUCCEEDED, {"ok": True})
        broker = CapabilityBroker(CapabilityRegistry((definition(),)), SafetyGate({"capability-1": AuthorityPolicy(frozenset({"needed"}))}), {"capability-1": execute})  # type: ignore[arg-type]
        outcome = broker.dispatch(CapabilityCall("call-1", "capability-1", {"value": 1}), authority())
        self.assertEqual(outcome.state.value, "rejected")
        self.assertEqual(calls[0], 0)

    def test_policy_change_alone_changes_authorization(self) -> None:
        call = CapabilityCall("call-1", "capability-1", {"value": 1})
        self.assertEqual(SafetyGate({"capability-1": AuthorityPolicy()}).evaluate(call, authority()).state, SafetyState.ALLOWED)
        self.assertEqual(SafetyGate({"capability-1": AuthorityPolicy(enabled=False)}).evaluate(call, authority()).state, SafetyState.DENIED)

    def test_malformed_results_and_executor_errors_are_failures_without_retry(self) -> None:
        registry = CapabilityRegistry((definition(),))
        gate = SafetyGate({"capability-1": AuthorityPolicy()})
        call = CapabilityCall("call-1", "capability-1", {"value": 1})
        bad = CapabilityBroker(registry, gate, {"capability-1": lambda data: CapabilityResult("other", "capability-1", CapabilityResultState.SUCCEEDED, {"ok": True})})
        self.assertEqual(bad.dispatch(call, authority()).reason, "result_identity_invalid")
        failing = CapabilityBroker(registry, gate, {"capability-1": lambda data: (_ for _ in ()).throw(RuntimeError())})
        self.assertEqual(failing.dispatch(call, authority()).reason, "executor_error")

    def test_input_and_result_failures_never_retry_or_fall_back(self) -> None:
        registry = CapabilityRegistry((definition(),))
        gate = SafetyGate({"capability-1": AuthorityPolicy()})
        calls = [0]
        def execute(data: object) -> CapabilityResult:
            calls[0] += 1
            return CapabilityResult("call-1", "capability-1", CapabilityResultState.SUCCEEDED, {"ok": "wrong"})
        broker = CapabilityBroker(registry, gate, {"capability-1": execute})  # type: ignore[arg-type]
        self.assertEqual(broker.dispatch(CapabilityCall("call-1", "capability-1", {"value": True}), authority()).reason, "input_invalid")
        self.assertEqual(calls[0], 0)
        self.assertEqual(broker.dispatch(CapabilityCall("call-1", "capability-1", {"value": 1}), authority()).reason, "result_output_invalid")
        self.assertEqual(calls[0], 1)
        self.assertEqual(CapabilityBroker(CapabilityRegistry(), gate, {}).dispatch(CapabilityCall("x", "unknown", {}), authority()).reason, "capability_unknown")
        self.assertEqual(CapabilityBroker(registry, gate, {}).dispatch(CapabilityCall("x", "capability-1", {"value": 1}), authority()).reason, "implementation_missing")

    def test_declared_failure_code_is_preserved_and_invalid_codes_are_rejected(self) -> None:
        registry = CapabilityRegistry((definition(),))
        gate = SafetyGate({"capability-1": AuthorityPolicy()})
        call = CapabilityCall("call-1", "capability-1", {"value": 1})
        good = CapabilityBroker(registry, gate, {"capability-1": lambda data: CapabilityResult("call-1", "capability-1", CapabilityResultState.FAILED, failure={"code": "unavailable"})})
        self.assertEqual(good.dispatch(call, authority()).result.failure["code"], "unavailable")
        bad = CapabilityBroker(registry, gate, {"capability-1": lambda data: CapabilityResult("call-1", "capability-1", CapabilityResultState.FAILED, failure={"code": "other"})})
        self.assertEqual(bad.dispatch(call, authority()).reason, "result_failure_invalid")

    def test_broker_reports_executor_invocation_truthfully_for_every_failure_stage(self) -> None:
        valid_call = CapabilityCall("call-1", "capability-1", {"value": 1})
        definition_registry = CapabilityRegistry((definition(),))
        allow = SafetyGate({"capability-1": AuthorityPolicy()})

        not_invoked = (
            (
                "unknown",
                CapabilityBroker(CapabilityRegistry(), allow, {}),
                valid_call,
                "capability_unknown",
                CapabilityAttemptDisposition.BROKER_FAILURE,
            ),
            (
                "invalid_input",
                CapabilityBroker(definition_registry, allow, {}),
                CapabilityCall("call-1", "capability-1", {"value": True}),
                "input_invalid",
                CapabilityAttemptDisposition.REJECTED,
            ),
            (
                "safety_rejection",
                CapabilityBroker(
                    definition_registry,
                    SafetyGate({"capability-1": AuthorityPolicy(enabled=False)}),
                    {},
                ),
                valid_call,
                "policy_denied",
                CapabilityAttemptDisposition.REJECTED,
            ),
            (
                "missing_implementation",
                CapabilityBroker(definition_registry, allow, {}),
                valid_call,
                "implementation_missing",
                CapabilityAttemptDisposition.BROKER_FAILURE,
            ),
        )
        for name, broker, call, reason, disposition in not_invoked:
            with self.subTest(stage=name):
                attempt = broker.dispatch(call, authority())
                self.assertFalse(attempt.implementation_invoked)
                self.assertEqual(attempt.reason, reason)
                self.assertEqual(attempt.disposition, disposition)

        invoked_results = {
            "executor_error": RuntimeError("failed"),
            "result_malformed": object(),
            "result_identity_invalid": CapabilityResult(
                "other",
                "capability-1",
                CapabilityResultState.SUCCEEDED,
                {"ok": True},
            ),
            "result_output_invalid": CapabilityResult(
                "call-1",
                "capability-1",
                CapabilityResultState.SUCCEEDED,
                {"ok": "wrong"},
            ),
            "result_failure_invalid": CapabilityResult(
                "call-1",
                "capability-1",
                CapabilityResultState.FAILED,
                failure={"code": "undeclared"},
            ),
        }
        for expected_reason, returned in invoked_results.items():
            with self.subTest(stage=expected_reason):
                calls = [0]

                def execute(data: object, value: object = returned) -> CapabilityResult:
                    calls[0] += 1
                    if isinstance(value, Exception):
                        raise value
                    return value  # type: ignore[return-value]

                attempt = CapabilityBroker(
                    definition_registry,
                    allow,
                    {"capability-1": execute},  # type: ignore[dict-item]
                ).dispatch(valid_call, authority())
                self.assertTrue(attempt.implementation_invoked)
                self.assertEqual(calls[0], 1)
                self.assertEqual(
                    attempt.disposition,
                    CapabilityAttemptDisposition.BROKER_FAILURE,
                )
                self.assertEqual(attempt.reason, expected_reason)

if __name__ == "__main__":
    unittest.main()
