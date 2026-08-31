"""One capability reads a document and commits the bill when nothing is unclear.

Before this, a routine bill was reasoned through one API call at a time: seven
Core calls and 143,036 estimated input tokens without a bill being created. The
extraction is now a bounded specialist question, supplier and coding come from
this organisation's own records, and the Core is asked only where judgment is
genuinely required.
"""

from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.contracts import CapabilityResultState, MailAttachment  # noqa: E402
from alx.tools import (  # noqa: E402
    CAPTURE_SUPPLIER_INVOICE,
    EXECUTE_XERO_BILL,
    build_xero_executors,
)

PDF = b"samtec-invoice-bytes"
DIGEST = hashlib.sha256(PDF).hexdigest()
TEXT = "SAMTEC INC\nInvoice 18300777\nTotal USD 180.00"


def extracted(**overrides) -> dict:
    values = {
        "document_type": "supplier_invoice",
        "supplier_name": "SAMTEC",
        "invoice_number": "18300777",
        "invoice_date": "2026-08-20",
        "due_date": "2026-09-20",
        "currency": "USD",
        "subtotal": "180.00",
        "tax_amount": "0.00",
        "total": "180.00",
        "description": "Electronic components",
        "verified": True,
        "problems": (),
    }
    values.update(overrides)
    return values


class FakeXero:
    def __init__(self) -> None:
        self.bills: dict[str, dict] = {}
        self.attachments: dict[str, list] = {}
        self.created = 0
        self.contacts = ({"Name": "SAMTEC", "ContactID": "c-1", "ContactStatus": "ACTIVE"},)
        self.history = (
            {
                "LineAmountTypes": "NoTax",
                "LineItems": [{"AccountCode": "310", "TaxType": "NONE"}],
            },
        )

    def search_contacts(self, _term):
        return self.contacts

    def bills_for_contact(self, _contact_id):
        return self.history

    def list_accounts(self):
        return ({"Code": "310", "Status": "ACTIVE", "TaxType": "NONE"},)

    def list_tax_rates(self):
        return ()

    def find_bill(self, invoice_number, contact_id=""):
        for bill in self.bills.values():
            if bill["InvoiceNumber"] == invoice_number:
                return bill
        return None

    def read_bill(self, invoice_id):
        return self.bills.get(invoice_id)

    def create_draft_bill(self, bill):
        self.created += 1
        invoice_id = f"bill-{self.created}"
        stored = {
            **bill,
            "InvoiceID": invoice_id,
            "Total": "180.00",
            "AmountDue": "180.00",
            "Contact": {**bill["Contact"], "Name": "SAMTEC"},
            "HasAttachments": False,
        }
        self.bills[invoice_id] = stored
        self.attachments[invoice_id] = []
        return stored

    def attach_bill_document(self, invoice_id, filename, media_type, content):
        self.attachments[invoice_id].append((filename, content))
        self.bills[invoice_id]["HasAttachments"] = True
        return {"FileName": filename}

    def list_bill_attachments(self, invoice_id):
        return tuple(
            {"AttachmentID": f"a-{i}", "FileName": n, "MimeType": "application/pdf"}
            for i, (n, _c) in enumerate(self.attachments.get(invoice_id, []))
        )

    def read_bill_attachment(self, invoice_id, attachment_id, _media_type):
        return self.attachments[invoice_id][int(attachment_id.split("-")[1])][1]

    def authorise_bill(self, invoice_id):
        self.bills[invoice_id]["Status"] = "AUTHORISED"
        return self.bills[invoice_id]

    def update_draft_bill(self, invoice_id, bill):
        self.bills[invoice_id] = {**self.bills[invoice_id], **bill}
        return self.bills[invoice_id]

    def delete_draft_bill(self, invoice_id):
        self.bills[invoice_id]["Status"] = "DELETED"
        return self.bills[invoice_id]


class FakeMail:
    def read_attachment(self, _reference, _attachment_id):
        return (
            MailAttachment("4", "invoice.pdf", "application/pdf", len(PDF), DIGEST, TEXT),
            PDF,
        )


def arguments(**overrides) -> dict:
    values = {
        "mailbox_id": "INBOX",
        "uid_validity": "777",
        "uid": "12",
        "attachment_id": "4",
        "expected_sha256": DIGEST,
        "context_line": "Invoice 18300777.pdf",
        "authorise": True,
    }
    values.update(overrides)
    return values


class RoutineCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.xero = FakeXero()
        self.calls: list[tuple[str, str]] = []

        def extractor(text: str, context_line: str) -> dict:
            self.calls.append((text, context_line))
            return extracted()

        self.executors = build_xero_executors(
            self.xero, FakeMail(), lambda: "call-1", extractor
        )
        self.capture = self.executors[CAPTURE_SUPPLIER_INVOICE]

    def test_the_specialist_is_actually_used_on_the_live_path(self) -> None:
        """Proof the wiring reaches the specialist, not a Core reasoning call."""
        self.capture(arguments())
        self.assertEqual(len(self.calls), 1)
        text, context_line = self.calls[0]
        self.assertEqual(text, TEXT)
        self.assertEqual(context_line, "Invoice 18300777.pdf")

    def test_a_known_supplier_bill_completes_in_one_capability_call(self) -> None:
        result = self.capture(arguments())
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        self.assertTrue(result.values["completed"])
        self.assertEqual(result.values["bill"]["status"], "AUTHORISED")
        self.assertEqual(self.xero.created, 1)
        self.assertEqual(result.values["attached"], ("invoice.pdf",))

    def test_the_whole_sequence_runs_without_returning_between_steps(self) -> None:
        """Every step happens inside one dispatch; no Core call in between."""
        result = self.capture(arguments())
        self.assertEqual(
            result.values["steps"],
            (
                "read_source_document",
                "extracted_invoice",
                "resolved_supplier",
                "looked_up_prior_coding",
                "executed_bill",
                "validated_supplied_values",
                "created_draft",
                "attached_and_verified_documents",
                "authorised",
                "read_back",
                "verified",
            ),
        )

    def test_prior_coding_is_applied_without_asking_a_model(self) -> None:
        self.capture(arguments())
        line = self.xero.bills["bill-1"]["LineItems"][0]
        self.assertEqual(line["AccountCode"], "310")
        self.assertEqual(line["TaxType"], "NONE")


class ReturnsToCoreTests(unittest.TestCase):
    """Only genuine ambiguity reaches AL/X, and it reaches her with the facts."""

    def build(self, extractor, **xero_changes):
        xero = FakeXero()
        for name, value in xero_changes.items():
            setattr(xero, name, value)
        self.xero = xero
        return build_xero_executors(
            xero, FakeMail(), lambda: "call-1", extractor
        )[CAPTURE_SUPPLIER_INVOICE]

    def test_a_document_that_is_not_an_invoice_stops_the_bill_path(self) -> None:
        capture = self.build(lambda *_: extracted(document_type="delivery_note"))
        result = capture(arguments())
        self.assertEqual(result.failure["code"], "not_an_invoice")
        self.assertEqual(self.xero.created, 0)

    def test_inconsistent_extraction_returns_the_problem_to_core(self) -> None:
        capture = self.build(
            lambda *_: extracted(verified=False, problems=("total missing",))
        )
        result = capture(arguments())
        self.assertFalse(result.values["completed"])
        self.assertEqual(result.values["returned_for"], "extraction_unverified")
        self.assertIn("total missing", result.values["detail"])
        self.assertEqual(self.xero.created, 0)

    def test_two_similar_suppliers_return_to_core(self) -> None:
        """The shuttle case: choosing between them is judgment."""
        capture = self.build(
            lambda *_: extracted(supplier_name="Cape Town Shuttle"),
            contacts=(
                {"Name": "Cape Town Shuttles and Tour", "ContactID": "c-1", "ContactStatus": "ACTIVE"},
                {"Name": "Cape Shuttle's and Tours", "ContactID": "c-2", "ContactStatus": "ACTIVE"},
            ),
        )
        result = capture(arguments())
        self.assertFalse(result.values["completed"])
        self.assertEqual(result.values["returned_for"], "supplier_unresolved")
        self.assertIn("several contacts match", result.values["detail"])
        self.assertEqual(self.xero.created, 0)

    def test_a_supplier_with_no_history_returns_to_core(self) -> None:
        capture = self.build(lambda *_: extracted(), history=())
        result = capture(arguments())
        self.assertFalse(result.values["completed"])
        self.assertEqual(result.values["returned_for"], "coding_unresolved")
        self.assertIn("no earlier bill", result.values["detail"])
        self.assertEqual(self.xero.created, 0)

    def test_conflicting_history_returns_to_core(self) -> None:
        capture = self.build(
            lambda *_: extracted(),
            history=(
                {"LineAmountTypes": "NoTax", "LineItems": [{"AccountCode": "310", "TaxType": "NONE"}]},
                {"LineAmountTypes": "Exclusive", "LineItems": [{"AccountCode": "429", "TaxType": "INPUT3"}]},
            ),
        )
        result = capture(arguments())
        self.assertFalse(result.values["completed"])
        self.assertEqual(result.values["returned_for"], "coding_unresolved")
        self.assertIn("disagree", result.values["detail"])
        self.assertEqual(self.xero.created, 0)

    def test_a_return_carries_the_extracted_facts_not_the_document(self) -> None:
        """Core gets the small relevant facts, not the whole invoice text."""
        capture = self.build(lambda *_: extracted(), history=())
        result = capture(arguments())
        invoice = result.values["invoice"]
        self.assertEqual(invoice["invoice_number"], "18300777")
        self.assertEqual(invoice["total"], "180.00")
        self.assertNotIn("SAMTEC INC\nInvoice", str(result.values))

    def test_a_hash_mismatch_never_reaches_the_specialist(self) -> None:
        calls = []
        capture = self.build(lambda *a: calls.append(a) or extracted())
        result = capture(arguments(expected_sha256="0" * 64))
        self.assertEqual(result.failure["code"], "source_mismatch")
        self.assertEqual(calls, [])


class NoFallbackTests(unittest.TestCase):
    """A routine bill cannot re-enter the old step-by-step planning loop."""

    def test_the_granular_write_steps_are_withheld_from_planning(self) -> None:
        import tempfile

        from alx.bootstrap.xero import RECOVERY_ONLY_CAPABILITIES, build_xero_runtime
        from alx.config import XeroSettings

        with tempfile.TemporaryDirectory() as directory:
            runtime = build_xero_runtime(
                XeroSettings(
                    "id", "secret", "http://localhost/callback", "", 10, 300, True, False
                ),
                Path(directory),
                FakeMail(),
                lambda: "call",
            )
        offered = {item.capability_id for item in runtime.definitions}
        self.assertIn(CAPTURE_SUPPLIER_INVOICE, offered)
        # execute_xero_bill is now reached through capture, not planned directly.
        self.assertIn(EXECUTE_XERO_BILL, RECOVERY_ONLY_CAPABILITIES)
        self.assertNotIn(EXECUTE_XERO_BILL, offered)

    def test_capture_arms_the_reasoning_ceiling_immediately(self) -> None:
        from alx.bootstrap.xero import BILL_TASK_CAPABILITIES

        self.assertIn(CAPTURE_SUPPLIER_INVOICE, BILL_TASK_CAPABILITIES)

    def test_the_live_runtime_supplies_the_specialist_extractor(self) -> None:
        source = (REPOSITORY_ROOT / "src/alx/bootstrap/live_voice.py").read_text()
        self.assertIn("ModelSpecialist(providers.reasoning)", source)
        self.assertIn("extract_invoice(", source)

    def test_capture_without_an_extractor_refuses_rather_than_planning(self) -> None:
        """A misconfigured runtime must not silently fall back to Core steps."""
        capture = build_xero_executors(
            FakeXero(), FakeMail(), lambda: "call-1"
        )[CAPTURE_SUPPLIER_INVOICE]
        result = capture(arguments())
        self.assertEqual(result.state, CapabilityResultState.FAILED)


if __name__ == "__main__":
    unittest.main()
