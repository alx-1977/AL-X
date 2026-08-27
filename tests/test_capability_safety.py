from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.capabilities import CapabilityBroker, CapabilityRegistry, DuplicateCapability  # noqa: E402
from alx.contracts import (  # noqa: E402
    Approval, ApprovalLifecycle, ApprovalScope, CapabilityCall, CapabilityDefinition,
    CapabilityResult, CapabilityResultState, SideEffect, StructuredSchema, ValueKind,
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

if __name__ == "__main__":
    unittest.main()
