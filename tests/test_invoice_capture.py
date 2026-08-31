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
        self.accounts = ({"Code": "310", "Status": "ACTIVE", "TaxType": "NONE"},)
        self.tax_rates = ()

    def search_contacts(self, _term):
        return self.contacts

    def bills_for_contact(self, _contact_id):
        return self.history

    def list_accounts(self):
        return self.accounts

    def list_tax_rates(self):
        return self.tax_rates

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


class RealDocumentTypeTests(unittest.TestCase):
    """The live failure: a genuine invoice refused as not_an_invoice."""

    def build(self, document_type: str):
        self.xero = FakeXero()
        return build_xero_executors(
            self.xero,
            FakeMail(),
            lambda: "call-1",
            lambda *_: extracted(document_type=document_type),
        )[CAPTURE_SUPPLIER_INVOICE]

    def test_a_document_calling_itself_an_invoice_is_captured(self) -> None:
        for document_type in ("invoice", "Invoice", "tax invoice", "supplier bill"):
            with self.subTest(document_type=document_type):
                capture = self.build(document_type)
                result = capture(arguments())
                self.assertTrue(
                    result.values["completed"],
                    "a real invoice was refused for its wording",
                )
                self.assertEqual(self.xero.created, 1)

    def test_a_statement_or_credit_note_still_stops_the_bill_path(self) -> None:
        for document_type in ("statement", "credit note", "quotation"):
            with self.subTest(document_type=document_type):
                capture = self.build(document_type)
                result = capture(arguments())
                self.assertEqual(result.failure["code"], "not_an_invoice")
                self.assertEqual(self.xero.created, 0)


class DefaultAccountTests(unittest.TestCase):
    """The live case: a supplier whose 18 bills used ten different treatments."""

    MIXED = (
        {"LineAmountTypes": "Exclusive",
         "LineItems": [{"AccountCode": "412", "TaxType": "NONE"}]},
        {"LineAmountTypes": "Inclusive",
         "LineItems": [{"AccountCode": "700", "TaxType": "CAPEXINPUT2"}]},
        {"LineAmountTypes": "Exclusive",
         "LineItems": [{"AccountCode": "720", "TaxType": "NONE"}]},
    )

    def build(self, *, history, tax_amount="0.00", default="310"):
        xero = FakeXero()
        xero.history = history
        xero.accounts = (
            {"Code": "310", "Status": "ACTIVE", "TaxType": "INPUT3"},
            {"Code": "412", "Status": "ACTIVE", "TaxType": "NONE"},
        )
        xero.tax_rates = ({"TaxType": "INPUT3", "Status": "ACTIVE"},)
        self.xero = xero
        return build_xero_executors(
            xero,
            FakeMail(),
            lambda: "call-1",
            lambda *_: extracted(tax_amount=tax_amount),
            default,
            "INPUT3",
        )[CAPTURE_SUPPLIER_INVOICE]

    def test_a_mixed_history_supplier_posts_without_asking(self) -> None:
        """Previously this returned coding_unresolved on every invoice."""
        capture = self.build(history=self.MIXED)
        result = capture(arguments())
        self.assertTrue(
            result.values["completed"],
            "a varied supplier must not interrogate Friedl on every invoice",
        )
        posted = self.xero.bills[result.values["bill"]["invoice_id"]]
        self.assertEqual(posted["LineItems"][0]["AccountCode"], "310")

    def test_a_zero_vat_invoice_is_posted_with_no_tax(self) -> None:
        capture = self.build(history=self.MIXED, tax_amount="0.00")
        result = capture(arguments())
        self.assertTrue(result.values["completed"])
        posted = self.xero.bills[result.values["bill"]["invoice_id"]]
        self.assertEqual(posted["LineItems"][0]["TaxType"], "NONE")
        self.assertEqual(posted["LineAmountTypes"], "NoTax")

    def test_settled_history_is_still_preferred_to_the_default(self) -> None:
        settled = (
            {"LineAmountTypes": "NoTax",
             "LineItems": [{"AccountCode": "412", "TaxType": "NONE"}]},
        ) * 2
        capture = self.build(history=settled)
        result = capture(arguments())
        self.assertTrue(result.values["completed"])
        posted = self.xero.bills[result.values["bill"]["invoice_id"]]
        self.assertEqual(posted["LineItems"][0]["AccountCode"], "412")

    def test_without_a_default_a_mixed_supplier_still_returns_to_alx(self) -> None:
        capture = self.build(history=self.MIXED, default="")
        result = capture(arguments())
        self.assertFalse(result.values["completed"])
        self.assertEqual(result.values["returned_for"], "coding_unresolved")


class ProcessedFilingTests(unittest.TestCase):
    """A captured invoice's mail is filed rather than left in the inbox."""

    def test_filing_needs_a_configured_mailbox(self) -> None:
        """The destination is configured, never chosen by AL/X."""
        from alx.tools import FILE_PROCESSED_MAIL_MESSAGE, build_mail_executors

        class Mail:
            def __init__(self) -> None:
                self.filed = []

            def file_message(self, reference, mailbox):
                self.filed.append((reference.uid, mailbox))
                return mailbox

        mail = Mail()
        unconfigured = build_mail_executors(
            mail, mail, lambda: "call-1"
        )[FILE_PROCESSED_MAIL_MESSAGE]
        result = unconfigured(
            {"mailbox_id": "INBOX", "uid_validity": "777", "uid": "12"}
        )
        self.assertEqual(result.failure["code"], "mailbox_unavailable")
        self.assertEqual(mail.filed, [], "nothing may move without a destination")

    def test_a_configured_mailbox_receives_the_message(self) -> None:
        from alx.tools import FILE_PROCESSED_MAIL_MESSAGE, build_mail_executors

        class Mail:
            def __init__(self) -> None:
                self.filed = []

            def file_message(self, reference, mailbox):
                self.filed.append((reference.uid, mailbox))
                return mailbox

        mail = Mail()
        configured = build_mail_executors(
            mail, mail, lambda: "call-1", processed_mailbox="FireFli/Processed"
        )[FILE_PROCESSED_MAIL_MESSAGE]
        result = configured(
            {"mailbox_id": "INBOX", "uid_validity": "777", "uid": "12"}
        )
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        self.assertTrue(result.values["filed"])
        self.assertEqual(result.values["mailbox_id"], "FireFli/Processed")
        self.assertEqual(mail.filed, [("12", "FireFli/Processed")])

    def test_filing_takes_no_destination_from_alx(self) -> None:
        """AL/X cannot file a message somewhere of her own choosing."""
        from alx.tools.mail import FILE_DEFINITION

        self.assertNotIn("mailbox", FILE_DEFINITION.input_schema.properties)
        self.assertEqual(
            set(FILE_DEFINITION.input_schema.properties),
            {"mailbox_id", "uid_validity", "uid"},
        )


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
                    "id", "secret", "http://localhost/callback", "", 10, 300, True, False, "", ""
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
        self.assertIn("ModelSpecialist(providers.specialist)", source)
        self.assertIn("extract_invoice(", source)

    def test_the_live_runtime_never_extracts_through_the_core_model(self) -> None:
        """A silent fallback to the Core is the cost this exists to avoid."""
        source = (REPOSITORY_ROOT / "src/alx/bootstrap/live_voice.py").read_text()
        self.assertNotIn("ModelSpecialist(providers.reasoning)", source)

    def test_an_unavailable_specialist_disables_extraction(self) -> None:
        from alx.bootstrap.providers import _build_reasoning_model
        from alx.config import ReasoningSettings

        settings = ReasoningSettings(
            "unknown-vendor", "m", "k", "https://example.test", 10, False,
            "default", "none",
        )
        self.assertIsNone(_build_reasoning_model(settings, None))

    def test_capture_without_an_extractor_refuses_rather_than_planning(self) -> None:
        """A misconfigured runtime must not silently fall back to Core steps."""
        capture = build_xero_executors(
            FakeXero(), FakeMail(), lambda: "call-1"
        )[CAPTURE_SUPPLIER_INVOICE]
        result = capture(arguments())
        self.assertEqual(result.state, CapabilityResultState.FAILED)


if __name__ == "__main__":
    unittest.main()
