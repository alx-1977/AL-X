"""Routine supplier invoices finish authorised, once, without needless reasoning.

A two-invoice run took ten Core calls: a standalone duplicate lookup before each
capture, which already checks for duplicates; each capture and each filing
chosen one reasoning step at a time; and both bills left as drafts, because
nothing AL/X saw said that authorising is what processing a supplier invoice
means or that the same authority covers it. These tests hold the mechanical
half of the fix. Whether AL/X chooses well is hers; what she is told, what a
result says, and what a plan does with it are code's.
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.capabilities import CapabilityBroker, CapabilityRegistry  # noqa: E402
from alx.contracts import (  # noqa: E402
    CapabilityAttempt,
    CapabilityAttemptDisposition,
    CapabilityCall,
    CapabilityDefinition,
    CapabilityResult,
    CapabilityResultState,
    ExecutionOutcome,
    ExecutionStep,
    PlanCondition,
    PlanOperation,
    PlanStatus,
    SideEffect,
)
from alx.bootstrap.xero import XERO_BILL_WRITE_PERMISSION, build_xero_runtime  # noqa: E402
from alx.core.model_reasoner import PROTOCOL_INSTRUCTIONS  # noqa: E402
from alx.safety import AuthorityContext, SafetyGate  # noqa: E402
from alx.tools import CAPTURE_SUPPLIER_INVOICE, build_xero_executors  # noqa: E402
from alx.tools.xero import (  # noqa: E402
    CAPTURE_INVOICE_DEFINITION,
    FIND_BILL_DEFINITION,
)
from support import xero_settings  # noqa: E402
from test_execution_plan import (  # noqa: E402
    SCHEMA,
    PlanHarness,
    Reasoner,
    install,
    plan,
    resolve,
)
from test_invoice_capture import FakeMail, FakeXero, arguments, extracted  # noqa: E402

FILE = "file_processed_mail_message"
DEFINITIONS = (
    CAPTURE_INVOICE_DEFINITION,
    CapabilityDefinition(FILE, FILE, SCHEMA, SCHEMA, SideEffect.EFFECTFUL),
)
FINISHED = (
    PlanCondition("values.completed", True),
    PlanCondition("values.bill.status", "AUTHORISED"),
)


class RecordingXero(FakeXero):
    def __init__(self) -> None:
        super().__init__()
        self.lookups: list[tuple[str, str]] = []

    def find_bill(self, invoice_number, contact_id=""):
        self.lookups.append((invoice_number, contact_id))
        return super().find_bill(invoice_number, contact_id)


def capture_step(uid: str, *, completion=FINISHED, authorise=True) -> ExecutionStep:
    return ExecutionStep(
        CapabilityCall(f"capture-{uid}", CAPTURE_SUPPLIER_INVOICE,
                       arguments(uid=uid, authorise=authorise)),
        tuple(completion),
    )


def file_step(uid: str) -> ExecutionStep:
    return ExecutionStep(CapabilityCall(f"file-{uid}", FILE, {"uid": uid}))


class PlannedCaptureTests(PlanHarness):
    """The real capture executor, run as decided plan steps."""

    def setUp(self):
        super().setUp()
        self.xero = RecordingXero()
        self.invoices = {"60110": extracted(invoice_number="2W54YRN2-0020"),
                         "60171": extracted(invoice_number="2W54YRN2-0021")}
        self.mail = FakeMail()
        self.executors = build_xero_executors(
            self.xero, self.mail, lambda: "executor-call",
            lambda *_: self.invoices[self.current_uid],
        )
        self.current_uid = ""

    def dispatch(self, call, state):
        if call.capability_id != CAPTURE_SUPPLIER_INVOICE:
            return super().dispatch(call, state)
        self.calls.append(call)
        self.current_uid = call.arguments["uid"]
        result = self.executors[CAPTURE_SUPPLIER_INVOICE](call.arguments)
        return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True,
                                 replace(result, call_id=call.call_id))

    def run_plan(self, *steps, then=()):
        reasoner = Reasoner(install(plan(*steps)), *then)
        agent = self.agent(reasoner, definitions=DEFINITIONS)
        self.person(agent)
        self.work(agent)
        return agent, reasoner

    def test_decided_captures_and_filing_run_without_core_between_steps(self):
        agent, reasoner = self.run_plan(
            capture_step("60110"), capture_step("60171"),
            file_step("60110"), file_step("60171"),
            then=(resolve(PlanOperation.FINISH, "Done."),),
        )
        self.assertEqual(self.plan_of().attention.reason, "plan_steps_done")
        self.assertEqual(reasoner.calls, 1, "a known step returned to the Core")
        self.assertEqual(self.names(), [CAPTURE_SUPPLIER_INVOICE] * 2 + [FILE] * 2)
        self.assertEqual({bill["Status"] for bill in self.xero.bills.values()},
                         {"AUTHORISED"})
        self.occasion(agent)
        self.assertEqual(reasoner.calls, 2)
        self.assertEqual(self.plan_of().status, PlanStatus.COMPLETED)

    def test_an_unresolved_supplier_stops_the_plan_before_filing(self):
        """No completion condition is needed for a return to reach her."""
        self.xero.contacts = ()
        _agent, reasoner = self.run_plan(
            capture_step("60110", completion=()), file_step("60110"))
        attention = self.plan_of().attention
        self.assertEqual(attention.reason, "planned_evidence_requires_judgement")
        self.assertEqual(len(attention.evidence_call_ids), 1)
        self.assertEqual(self.names(), [CAPTURE_SUPPLIER_INVOICE])
        self.assertEqual(reasoner.calls, 1)
        self.assertEqual(self.xero.bills, {})

    def test_an_existing_authorised_bill_stops_the_plan_before_filing(self):
        self.current_uid = "60110"
        self.executors[CAPTURE_SUPPLIER_INVOICE](arguments(uid="60110"))
        created = self.xero.created
        self.run_plan(capture_step("60110", completion=()), file_step("60110"))
        self.assertEqual(self.plan_of().attention.reason,
                         "planned_evidence_requires_judgement")
        self.assertEqual(self.names(), [CAPTURE_SUPPLIER_INVOICE])
        self.assertEqual(self.xero.created, created, "a duplicate reached Xero")
        returned = self.state().attempts[-1].result.values
        self.assertEqual(returned["returned_for"], "duplicate_bill")

    def test_a_failed_capture_stops_the_plan(self):
        self.mail.digest = "0" * 64
        self.run_plan(capture_step("60110"), file_step("60110"))
        self.assertEqual(self.plan_of().attention.reason, "planned_result_failed")
        self.assertEqual(self.names(), [CAPTURE_SUPPLIER_INVOICE])

    def test_a_draft_does_not_satisfy_a_finished_bill(self):
        self.run_plan(capture_step("60110", authorise=False), file_step("60110"))
        self.assertEqual(self.plan_of().attention.reason, "planned_result_unexpected")
        self.assertEqual(self.names(), [CAPTURE_SUPPLIER_INVOICE])
        bill = self.state().attempts[-1].result.values["bill"]
        self.assertEqual(bill["status"], "DRAFT")


class CaptureContractTests(unittest.TestCase):
    """What AL/X is told, and what a capture reports, about a routine bill."""

    def test_the_catalogue_says_authorising_is_the_finish_and_needs_no_more_authority(self):
        purpose = CAPTURE_INVOICE_DEFINITION.purpose
        self.assertIn("authorise true finishes the bill AUTHORISED", purpose)
        self.assertIn("unfinished DRAFT", purpose)
        self.assertIn("one authority", purpose)
        self.assertIn("authorise", CAPTURE_INVOICE_DEFINITION.input_schema.required)

    def test_standing_authority_authorises_without_a_new_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_xero_runtime(
                xero_settings(unattended_bill_writes=True), Path(directory),
                FakeMail(), lambda: "call", lambda *_: extracted(),
            )
        policy = runtime.policies[CAPTURE_SUPPLIER_INVOICE]
        self.assertFalse(policy.approval_required)
        xero = FakeXero()
        broker = CapabilityBroker(
            CapabilityRegistry((CAPTURE_INVOICE_DEFINITION,)),
            SafetyGate({CAPTURE_SUPPLIER_INVOICE: policy}),
            build_xero_executors(xero, FakeMail(), lambda: "capture-1",
                                 lambda *_: extracted()),
        )
        attempt = broker.dispatch(
            CapabilityCall("capture-1", CAPTURE_SUPPLIER_INVOICE, arguments(authorise=True)),
            AuthorityContext("friedl", frozenset({XERO_BILL_WRITE_PERMISSION}),
                             datetime.now(UTC)),
        )
        self.assertEqual(attempt.disposition, CapabilityAttemptDisposition.EXECUTED)
        self.assertEqual(attempt.result.values["bill"]["status"], "AUTHORISED")

    def test_capture_is_its_own_duplicate_check(self):
        self.assertIn("No separate lookup is needed first", CAPTURE_INVOICE_DEFINITION.purpose)
        self.assertIn("not a step before capturing", FIND_BILL_DEFINITION.purpose)
        xero = RecordingXero()
        capture = build_xero_executors(xero, FakeMail(), lambda: "c",
                                       lambda *_: extracted())[CAPTURE_SUPPLIER_INVOICE]
        self.assertTrue(capture(arguments()).values["completed"])
        # Any contact first, then the resolved supplier: the D-018 pair.
        self.assertEqual(xero.lookups, [("18300777", ""), ("18300777", "c-1")])
        again = capture(arguments())
        self.assertEqual(again.values["returned_for"], "duplicate_bill")
        self.assertEqual(xero.created, 1)

    def test_a_routine_bill_is_success_and_a_return_needs_judgement(self):
        capture = build_xero_executors(FakeXero(), FakeMail(), lambda: "c",
                                       lambda *_: extracted())[CAPTURE_SUPPLIER_INVOICE]
        finished = capture(arguments())
        self.assertIsNone(finished.outcome)
        returned = capture(arguments())
        self.assertFalse(returned.values["completed"])
        self.assertIs(returned.outcome, ExecutionOutcome.AMBIGUOUS)
        unresolved = FakeXero()
        unresolved.contacts = ()
        early = build_xero_executors(unresolved, FakeMail(), lambda: "c",
                                     lambda *_: extracted())[CAPTURE_SUPPLIER_INVOICE]
        result = early(arguments())
        self.assertEqual(result.values["returned_for"], "supplier_unresolved")
        self.assertIs(result.outcome, ExecutionOutcome.AMBIGUOUS)


class ContactScopedXero(RecordingXero):
    """Honours the contact filter, as Xero's ContactIDs query does."""

    def find_bill(self, invoice_number, contact_id=""):
        self.lookups.append((invoice_number, contact_id))
        for bill in self.bills.values():
            if bill["InvoiceNumber"] == invoice_number and (
                not contact_id or bill["Contact"]["ContactID"] == contact_id
            ):
                return bill
        return None


class DuplicateTests(unittest.TestCase):
    """Capture alone catches the invoice number under any contact."""

    def setUp(self) -> None:
        self.xero = ContactScopedXero()
        self.capture = build_xero_executors(
            self.xero, FakeMail(), lambda: "c", lambda *_: extracted()
        )[CAPTURE_SUPPLIER_INVOICE]

    def existing(self, contact_id: str, status: str) -> None:
        self.xero.bills["bill-0"] = {
            "InvoiceID": "bill-0", "InvoiceNumber": "18300777", "Status": status,
            "Contact": {"ContactID": contact_id, "Name": "Other Supplier"},
            "Total": "180.00", "AmountDue": "180.00", "HasAttachments": True,
        }

    def test_no_duplicate_proceeds_normally(self):
        result = self.capture(arguments())
        self.assertTrue(result.values["completed"])
        self.assertEqual(result.values["bill"]["status"], "AUTHORISED")
        self.assertEqual(self.xero.created, 1)

    def test_same_supplier_and_number_keeps_existing_duplicate_handling(self):
        self.existing("c-1", "AUTHORISED")
        result = self.capture(arguments())
        self.assertEqual(result.values["returned_for"], "duplicate_bill")
        self.assertIs(result.outcome, ExecutionOutcome.AMBIGUOUS)
        self.assertEqual(self.xero.created, 0)

    def test_same_number_under_another_contact_returns_for_judgement(self):
        for status in ("AUTHORISED", "DRAFT"):
            with self.subTest(status=status):
                self.xero.bills.clear()
                self.existing("c-9", status)
                result = self.capture(arguments())
                self.assertFalse(result.values["completed"])
                self.assertEqual(result.values["returned_for"],
                                 "invoice_number_under_other_contact")
                self.assertEqual(result.values["bill"]["contact_id"], "c-9")
                self.assertIs(result.outcome, ExecutionOutcome.AMBIGUOUS)
                self.assertEqual(self.xero.created, 0)
                self.assertEqual(self.xero.bills["bill-0"]["Status"], status)


class ReferenceTests(unittest.TestCase):
    """The bill's reference comes from evidence, never from a bare file name."""

    def reference(self, invoice, **inputs) -> str:
        xero = FakeXero()
        capture = build_xero_executors(
            xero, FakeMail(filename="Invoice-2W54YRN2-0021.pdf"), lambda: "c",
            lambda *_: invoice,
        )[CAPTURE_SUPPLIER_INVOICE]
        result = capture(arguments(**inputs))
        self.assertTrue(result.values["completed"], result)
        return xero.bills["bill-1"]["Reference"]

    def test_the_context_line_is_the_reference_when_given(self):
        self.assertEqual(
            self.reference(extracted(), context_line="Anthropic invoice 2W54YRN2-0021"),
            "Anthropic invoice 2W54YRN2-0021",
        )

    def test_without_a_context_line_the_invoice_describes_itself(self):
        values = arguments()
        del values["context_line"]
        xero = FakeXero()
        capture = build_xero_executors(
            xero, FakeMail(filename="Invoice-2W54YRN2-0021.pdf"), lambda: "c",
            lambda *_: extracted(description="Team plan, 1 Oct to 1 Nov 2026"),
        )[CAPTURE_SUPPLIER_INVOICE]
        self.assertTrue(capture(values).values["completed"])
        self.assertEqual(xero.bills["bill-1"]["Reference"], "Team plan, 1 Oct to 1 Nov 2026")

    def test_a_blank_context_line_is_not_a_reference(self):
        self.assertEqual(
            self.reference(extracted(description="Team plan"), context_line="   "),
            "Team plan",
        )

    def test_the_file_name_remains_only_when_the_invoice_states_nothing(self):
        self.assertEqual(
            self.reference(extracted(description=""), context_line=""),
            "Invoice-2W54YRN2-0021.pdf",
        )


class CompletionGuidanceTests(unittest.TestCase):
    """Routine completions are brief by general guidance, not invoice wording."""

    def test_finished_work_is_reported_briefly(self):
        text = " ".join(PROTOCOL_INSTRUCTIONS.split())
        self.assertIn("When work he asked for finishes as expected, the outcome is the "
                      "answer: say that it is done, briefly.", text)
        self.assertIn("Identifiers, amounts, dates, the checks you ran and where things "
                      "went stay in the background unless something was unusual, he "
                      "needs to act, or he asks.", text)

    def test_decided_calls_are_steered_to_a_plan(self):
        text = " ".join(PROTOCOL_INSTRUCTIONS.split())
        self.assertIn("Once the remaining calls are decided, prefer one plan", text)

    def test_the_protocol_carries_no_domain_specific_wording(self):
        for word in ("invoice", "xero", "bill", "supplier", "anthropic", "receipt"):
            with self.subTest(word=word):
                self.assertIsNone(
                    re.search(rf"\b{word}s?\b", PROTOCOL_INSTRUCTIONS, re.IGNORECASE))


if __name__ == "__main__":
    unittest.main()
