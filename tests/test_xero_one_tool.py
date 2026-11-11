"""Accounting as one capability with an action per job (D-074, amended 2026-11-09).

Fourteen capabilities became `xero`: the Xero contact, account, tax-rate, bill
and quote jobs, supplier-invoice and DigiKey capture, the DHL import and the
DigiKey order read. Each action keeps its own contract and, through the one
policy, the approval its kind of write had under Friedl's settings (D-018,
D-019, the DHL setting). Results pass through unwrapped.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alx.bootstrap.dhl import DHL_IMPORT_PERMISSION  # noqa: E402
from alx.bootstrap.digikey import DIGIKEY_ORDER_READ_PERMISSION  # noqa: E402
from alx.bootstrap.mail import captured_invoice_filing_scopes, dhl_mail_scopes  # noqa: E402
from alx.bootstrap.xero import (  # noqa: E402
    BILL_TASK_ACTIONS,
    XERO_BILL_DELETE_PERMISSION,
    XERO_BILL_WRITE_PERMISSION,
    XERO_CONTACT_CREATE_PERMISSION,
    XERO_CONTACT_RENAME_PERMISSION,
    XERO_QUOTE_INVOICE_PERMISSION,
    XERO_READ_PERMISSION,
    build_xero_runtime,
)
from alx.capabilities import CapabilityBroker, CapabilityRegistry  # noqa: E402
from alx.contracts import (  # noqa: E402
    CapabilityAttempt,
    CapabilityAttemptDisposition,
    CapabilityCall,
    CapabilityResult,
    CapabilityResultState,
    ExecutionOutcome,
    GoalState,
    Objective,
    SideEffect,
    SuccessCriterion,
    XeroAction,
    recorded_xero_action,
)
from alx.contracts.xero import _RECORDED_BEFORE_D074  # noqa: E402
from alx.core.loop import CoreAgent  # noqa: E402
from alx.core.model_reasoner import _one_line  # noqa: E402
from alx.safety import AuthorityContext, SafetyGate  # noqa: E402
from alx.tools import XERO, xero_definition  # noqa: E402
from alx.tools import xero as xero_tools  # noqa: E402
from alx.tools.worker import WORKER_CAPABILITIES, WORKER_LIMITS  # noqa: E402
from alx.tools.xero import CONTRACTS, READ_ACTIONS  # noqa: E402
from support import xero_settings  # noqa: E402
from test_invoice_capture import FakeMail, FakeXero, arguments, extracted  # noqa: E402
from xero_actions import (  # noqa: E402
    CAPTURE_DIGIKEY_BILL,
    CAPTURE_INVOICE,
    DELETE_DRAFT_BILL,
    IMPORT_DHL,
    LOOKUP_DIGIKEY,
    dhl_import,
    needs_approval,
    xero_arguments,
    xero_call,
)

OLD_IDS = (
    "search_xero_contacts", "create_xero_contact", "update_xero_contact",
    "list_xero_accounts", "list_xero_tax_rates", "find_xero_bill", "read_xero_bill",
    "delete_xero_draft_bill", "capture_supplier_invoice", "capture_digikey_order_bill",
    "process_dhl_import", "find_xero_quotes", "invoice_xero_quote", "lookup_digikey_order",
)
FULL_LINE = (
    "Xero accounting: read contacts, accounts, tax rates, bills, quotes and DigiKey "
    "orders, bill supplier invoices, DigiKey orders and DHL imports, delete draft "
    "bills, add/rename contacts, invoice quotes."
)
NOW = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)


def runtime(*, capture=True, digikey=True, dhl=True, **settings):
    configured = xero_settings(**settings)
    with tempfile.TemporaryDirectory() as directory:
        return build_xero_runtime(
            configured, Path(directory), FakeMail(), lambda: "call-1",
            (lambda *_: extracted()) if capture else None,
            digikey_lookup=(lambda **_: {}) if digikey else None,
            dhl=dhl_import(configured, FakeMail()) if dhl else None,
        )


class OneCapabilityTests(unittest.TestCase):
    def test_the_runtime_registers_one_xero_capability(self) -> None:
        built = runtime()
        self.assertEqual([item.capability_id for item in built.definitions], [XERO])
        self.assertEqual(set(built.executors), {XERO})
        self.assertEqual(set(built.policies), {XERO})
        self.assertEqual(set(built.actions), set(XeroAction))

    def test_no_superseded_id_remains_in_production_code(self) -> None:
        """Law 0: the fourteen ids are gone, not hidden, wrapped or renamed.
        They are only recognised in goals saved before D-074, in one place,
        and none can be called."""
        recognised = ROOT / "src/alx/contracts/xero.py"
        for path in (ROOT / "src/alx").rglob("*.py"):
            if path == recognised:
                continue
            source = path.read_text(encoding="utf-8")
            for old in OLD_IDS:
                with self.subTest(path=path.name, old=old):
                    self.assertNotIn(old, source)
        self.assertEqual(set(_RECORDED_BEFORE_D074), set(OLD_IDS))
        built = runtime()
        for old in OLD_IDS:
            with self.subTest(old=old):
                self.assertNotIn(old, built.executors)
                self.assertNotIn(old, built.policies)
                self.assertNotIn(old, {item.value for item in XeroAction})
                refused = built.executors[XERO]({"action": old})
                self.assertEqual(refused.failure["code"], "arguments_unusable")
        for name in dir(xero_tools):
            self.assertFalse(name.endswith(("_XERO_BILL", "_XERO_CONTACT", "_XERO_CONTACTS",
                                            "_XERO_QUOTE", "_XERO_QUOTES", "_SUPPLIER_INVOICE")),
                             name)

    def test_the_catalogue_line_names_every_offered_job(self) -> None:
        line = _one_line(xero_definition(tuple(XeroAction)).purpose)
        self.assertEqual(line, FULL_LINE)
        self.assertLessEqual(len(line), 200, "the catalogue line is cut at 200 characters")

    def test_each_action_keeps_its_own_contract_and_lists_its_fields(self) -> None:
        definition = xero_definition(tuple(XeroAction))
        self.assertEqual(set(CONTRACTS), set(XeroAction))
        self.assertEqual(definition.input_schema.required, ("action",))
        self.assertFalse(definition.input_schema.extra_properties)
        self.assertIs(definition.side_effect, SideEffect.EFFECTFUL)
        for action, contract in CONTRACTS.items():
            with self.subTest(action=action):
                self.assertIn(f"[{action.value}] fields: ", definition.purpose)
                self.assertIn(contract.purpose, definition.purpose)
                self.assertTrue(set(contract.input_schema.properties)
                                <= set(definition.input_schema.properties))
                self.assertTrue(set(contract.output_schema.properties)
                                <= set(definition.output_schema.properties))
                self.assertTrue(set(contract.possible_failure_codes)
                                <= set(definition.possible_failure_codes))


class OfferedActionsTests(unittest.TestCase):
    def assert_absent(self, built, *actions: str) -> None:
        (definition,) = built.definitions
        offered = {item.value for item in built.actions}
        for action in actions:
            with self.subTest(action=action):
                self.assertNotIn(action, offered)
                self.assertNotIn(f"[{action}]", definition.purpose)
                result = built.executors[XERO]({"action": action})
                self.assertEqual(result.failure["code"], "arguments_unusable")
                self.assertNotIn(action, result.failure["reason"])
                for other in offered:
                    self.assertIn(other, result.failure["reason"])

    def test_no_extractor_no_capture(self) -> None:
        built = runtime(capture=False)
        self.assert_absent(built, CAPTURE_INVOICE)
        self.assertNotIn("supplier invoices", _one_line(built.definitions[0].purpose))
        self.assertIn(CAPTURE_DIGIKEY_BILL, {item.value for item in built.actions})

    def test_no_digikey_no_digikey_actions(self) -> None:
        built = runtime(digikey=False)
        self.assert_absent(built, CAPTURE_DIGIKEY_BILL, LOOKUP_DIGIKEY)
        self.assertNotIn("DigiKey", _one_line(built.definitions[0].purpose))
        self.assertNotIn(DIGIKEY_ORDER_READ_PERMISSION, built.permissions)

    def test_no_dhl_no_import(self) -> None:
        built = runtime(dhl=False)
        self.assert_absent(built, IMPORT_DHL)
        self.assertNotIn("DHL", _one_line(built.definitions[0].purpose))
        self.assertNotIn(DHL_IMPORT_PERMISSION, built.permissions)

    def test_nothing_optional_leaves_the_core_jobs(self) -> None:
        built = runtime(capture=False, digikey=False, dhl=False)
        self.assertEqual(
            _one_line(built.definitions[0].purpose),
            "Xero accounting: read contacts, accounts, tax rates, bills and quotes, "
            "delete draft bills, add/rename contacts, invoice quotes.",
        )
        # No capture offered, so no bill-write permission is asked for.
        self.assertEqual(built.policies[XERO].permission_references, frozenset({
            XERO_READ_PERMISSION, XERO_BILL_DELETE_PERMISSION,
            XERO_CONTACT_RENAME_PERMISSION, XERO_CONTACT_CREATE_PERMISSION,
            XERO_QUOTE_INVOICE_PERMISSION,
        }))

    def test_the_policy_holds_the_permissions_of_the_offered_actions(self) -> None:
        built = runtime()
        self.assertEqual(built.policies[XERO].permission_references, frozenset({
            XERO_READ_PERMISSION, XERO_BILL_WRITE_PERMISSION, XERO_BILL_DELETE_PERMISSION,
            XERO_CONTACT_RENAME_PERMISSION, XERO_CONTACT_CREATE_PERMISSION,
            XERO_QUOTE_INVOICE_PERMISSION, DHL_IMPORT_PERMISSION,
            DIGIKEY_ORDER_READ_PERMISSION,
        }))
        self.assertEqual(built.permissions, built.policies[XERO].permission_references)


class ApprovalFollowsSettingsTests(unittest.TestCase):
    WRITES_AND_DELETES = (CAPTURE_INVOICE, CAPTURE_DIGIKEY_BILL, IMPORT_DHL, DELETE_DRAFT_BILL)

    def test_attended_settings_ask_for_capture_delete_and_dhl(self) -> None:
        built = runtime()
        self.assertTrue(built.policies[XERO].approval_required)
        for action in self.WRITES_AND_DELETES:
            with self.subTest(action=action):
                self.assertTrue(needs_approval(built, action))
        for action in ("create_contact", "update_contact", "invoice_quote",
                       *(item.value for item in READ_ACTIONS)):
            with self.subTest(action=action):
                self.assertFalse(needs_approval(built, action))

    def test_unattended_settings_ask_for_nothing(self) -> None:
        built = runtime(unattended_bill_writes=True, unattended_bill_deletes=True)
        for action in XeroAction:
            with self.subTest(action=action):
                self.assertFalse(needs_approval(built, action.value))

    def test_each_setting_governs_only_its_own_kind_of_write(self) -> None:
        writes = runtime(unattended_bill_writes=True)
        self.assertFalse(needs_approval(writes, CAPTURE_INVOICE))
        self.assertFalse(needs_approval(writes, CAPTURE_DIGIKEY_BILL))
        self.assertFalse(needs_approval(writes, IMPORT_DHL))
        self.assertTrue(needs_approval(writes, DELETE_DRAFT_BILL))
        deletes = runtime(unattended_bill_deletes=True)
        self.assertTrue(needs_approval(deletes, CAPTURE_INVOICE))
        self.assertTrue(needs_approval(deletes, IMPORT_DHL))
        self.assertFalse(needs_approval(deletes, DELETE_DRAFT_BILL))

    def test_an_attended_approval_covers_only_the_exact_call(self) -> None:
        built = runtime()
        from alx.contracts import Approval, ApprovalLifecycle, ApprovalScope
        from datetime import timedelta
        approved = xero_arguments(DELETE_DRAFT_BILL, {"invoice_id": "b-1", "invoice_number": "1",
                                                      "expected_total": "1.00"})
        approval = Approval("a-1", ApprovalScope(XERO, approved), ApprovalLifecycle.GRANTED,
                            NOW + timedelta(minutes=5))
        gate = SafetyGate(built.policies)
        context = AuthorityContext("friedl", built.permissions, NOW, (approval,))
        exact = CapabilityCall("c-1", XERO, approved, "a-1")
        other = CapabilityCall("c-2", XERO, {**approved, "action": CAPTURE_INVOICE}, "a-1")
        self.assertEqual(gate.evaluate(exact, context).reason, "approved")
        self.assertEqual(gate.evaluate(other, context).reason, "approval_invalid")


class EffectByActionTests(unittest.TestCase):
    def test_reads_change_nothing_and_the_rest_change_xero(self) -> None:
        definition = xero_definition(tuple(XeroAction))
        reads = {"search_contacts", "list_accounts", "list_tax_rates", "find_bill",
                 "read_bill", "find_quotes", "lookup_digikey"}
        self.assertEqual({item.value for item in READ_ACTIONS}, reads)
        for action in XeroAction:
            with self.subTest(action=action):
                expected = SideEffect.NONE if action.value in reads else SideEffect.EFFECTFUL
                self.assertIs(definition.effect_for(xero_arguments(action.value)), expected)
        self.assertIs(definition.effect_for({"action": "pay_bill"}), SideEffect.EFFECTFUL)

    def test_reads_need_no_active_goal_but_writes_do(self) -> None:
        definition = xero_definition(tuple(XeroAction))
        core = CoreAgent.__new__(CoreAgent)
        core._capabilities = (definition,)
        core._has_pending_dispatch = lambda state: False
        core._definition = lambda capability_id: definition
        for action in READ_ACTIONS:
            with self.subTest(action=action):
                self.assertIsNone(core._dispatch_blocked_reason(xero_call("c", action.value), None))
        for action in (CAPTURE_INVOICE, DELETE_DRAFT_BILL, IMPORT_DHL, "create_contact"):
            with self.subTest(action=action):
                self.assertEqual(core._dispatch_blocked_reason(xero_call("c", action), None),
                                 "active_goal_required")


class RedundantApprovalTests(unittest.TestCase):
    """An action exempt under the settings needs no approval, so a volunteered
    one is dropped rather than checked against Friedl's latest turn: in a
    background turn that check refused a capture that needed no approval."""

    def core(self, built):
        (definition,) = built.definitions
        core = CoreAgent.__new__(CoreAgent)
        core._definition = lambda capability_id: definition
        core._approval_free_capabilities = frozenset({"some_other_capability"})
        core._approval_exempt_when = {XERO: built.policies[XERO].approval_exempt_when}
        return core

    def decision(self, action):
        from alx.contracts import AgentDecision
        return AgentDecision(call=xero_call("c-1", action, arguments(), "approval-1"))

    def test_an_exempt_write_drops_volunteered_approval(self) -> None:
        core = self.core(runtime(unattended_bill_writes=True))
        kept = core._without_redundant_approval(self.decision(CAPTURE_INVOICE))
        self.assertIsNone(kept.call.approval_id)

    def test_a_write_that_needs_approval_keeps_it(self) -> None:
        core = self.core(runtime())
        kept = core._without_redundant_approval(self.decision(CAPTURE_INVOICE))
        self.assertEqual(kept.call.approval_id, "approval-1")


class ExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.xero = FakeXero()
        self.built = runtime(unattended_bill_writes=True, unattended_bill_deletes=True)
        from alx.tools import build_xero_executors
        self.broker = CapabilityBroker(
            CapabilityRegistry(self.built.definitions),
            SafetyGate(self.built.policies),
            build_xero_executors(self.xero, FakeMail(), lambda: "capture-1",
                                 lambda *_: extracted()),
        )

    def test_a_capture_result_passes_through_unwrapped(self) -> None:
        attempt = self.broker.dispatch(
            xero_call("capture-1", CAPTURE_INVOICE, arguments()),
            AuthorityContext("friedl", self.built.permissions, NOW),
        )
        self.assertIs(attempt.disposition, CapabilityAttemptDisposition.EXECUTED)
        result = attempt.result
        self.assertEqual(result.capability_id, XERO)
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
        # The capture's own fields at the top, not under a wrapper.
        self.assertTrue(result.values["completed"])
        self.assertEqual(result.values["bill"]["status"], "AUTHORISED")
        self.assertTrue(result.recovery_values()["completed"])
        self.assertIsNone(result.outcome)
        returned = self.broker.dispatch(
            xero_call("capture-1", CAPTURE_INVOICE, arguments()),
            AuthorityContext("friedl", self.built.permissions, NOW),
        ).result
        self.assertEqual(returned.values["returned_for"], "duplicate_bill")
        self.assertIs(returned.outcome, ExecutionOutcome.AMBIGUOUS)

    def test_a_digikey_order_read_keeps_its_provenance(self) -> None:
        from alx.tools import build_xero_executors
        executors = build_xero_executors(FakeXero(), FakeMail(), lambda: "read-1",
                                         digikey_lookup=lambda **_: _order())
        result = executors[XERO](xero_arguments(LOOKUP_DIGIKEY, {"sales_order_number": "1"}))
        self.assertEqual(result.capability_id, XERO)
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(result.values["sales_order_number"], "1")
        self.assertIsNotNone(result.provenance)

    def test_each_action_is_held_to_its_own_fields(self) -> None:
        for action, values in (
            (CAPTURE_INVOICE, {**arguments(), "invoice_id": "b-1"}),
            (DELETE_DRAFT_BILL, {"invoice_id": "b-1"}),
            ("read_bill", {"search_term": "x"}),
        ):
            with self.subTest(action=action):
                result = self.broker.dispatch(
                    xero_call("capture-1", action, values),
                    AuthorityContext("friedl", self.built.permissions, NOW),
                ).result
                self.assertEqual(result.failure["code"], "arguments_unusable")
        self.assertEqual(self.xero.created, 0)


def _order() -> dict:
    return {"sales_order_number": "1", "status": "Shipped", "invoice_date": "",
            "currency": "ZAR", "subtotal": "1", "shipping": "0", "tax": "0", "total": "1",
            "invoice_numbers": [], "shipments": [], "line_items": []}


class SavedBeforeD074Tests(unittest.TestCase):
    def test_every_former_id_is_recognised_and_today_s_form_too(self) -> None:
        for old, action in _RECORDED_BEFORE_D074.items():
            with self.subTest(old=old):
                self.assertIs(recorded_xero_action(old, {}), action)
                self.assertIs(recorded_xero_action(XERO, {"action": action.value}), action)
        self.assertIsNone(recorded_xero_action(XERO, {"action": "pay_bill"}))
        self.assertIsNone(recorded_xero_action("mail", {"action": "read"}))
        self.assertIn(recorded_xero_action("process_dhl_import", {}), BILL_TASK_ACTIONS)

    def state(self, capability_id: str, arguments: dict, values: dict) -> GoalState:
        call = CapabilityCall("c-1", capability_id, arguments)
        attempt = CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True,
                                    CapabilityResult("c-1", capability_id,
                                                     CapabilityResultState.SUCCEEDED, values))
        return GoalState("goal-1", Objective("turn:1", "bills"),
                         (SuccessCriterion("c1", "posted"),), attempts=(attempt,))

    def test_saved_captures_and_imports_still_authorise_filing(self) -> None:
        reference = {"mailbox_id": "INBOX", "uid_validity": "7", "uid": "2"}
        done = {"completed": True, "stage": "customs_documents", "source_mail": [reference]}
        for capability_id, arguments in (
            ("capture_supplier_invoice", reference),
            (XERO, xero_arguments(CAPTURE_INVOICE, reference)),
        ):
            with self.subTest(capability_id=capability_id):
                self.assertTrue(captured_invoice_filing_scopes(
                    self.state(capability_id, arguments, {"completed": True})))
        for capability_id, arguments in (
            ("process_dhl_import", {"documents": []}),
            (XERO, xero_arguments(IMPORT_DHL, {"documents": []})),
        ):
            with self.subTest(capability_id=capability_id):
                self.assertTrue(dhl_mail_scopes(self.state(capability_id, arguments, done)))


class WorkerTests(unittest.TestCase):
    def test_the_worker_has_no_accounting_reach(self) -> None:
        """No Xero capability was in the worker's list before D-074, so none is now."""
        self.assertNotIn(XERO, WORKER_CAPABILITIES)
        self.assertNotIn(XERO, {capability for capability, _ in WORKER_LIMITS})


if __name__ == "__main__":
    unittest.main()
