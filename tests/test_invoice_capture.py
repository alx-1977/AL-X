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
from alx.contracts import SideEffect  # noqa: E402
from support import xero_settings  # noqa: E402
from alx.tools import (  # noqa: E402
    CAPTURE_SUPPLIER_INVOICE,
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


class StaleDraftTests(unittest.TestCase):
    """Review finding: an altered draft was authorised as correct.

    Resuming checked only supplier, invoice number, status and total, so a
    draft whose account had been changed to something else was authorised and
    reported complete. Matching the total is not enough to commit to a bill.
    """

    def build(self):
        self.xero = FakeXero()
        self.xero.accounts = ({"Code": "310", "Status": "ACTIVE", "TaxType": "NONE"},)
        return build_xero_executors(
            self.xero, FakeMail(), lambda: "call-1", lambda *_: extracted(), "310", "INPUT3"
        )[CAPTURE_SUPPLIER_INVOICE]

    def existing_draft(self, **changes):
        capture = self.build()
        capture(arguments())
        invoice_id = next(iter(self.xero.bills))
        self.xero.bills[invoice_id]["Status"] = "DRAFT"
        self.xero.bills[invoice_id].update(changes)
        return capture, invoice_id

    def test_a_draft_with_a_changed_account_is_never_authorised(self) -> None:
        capture, invoice_id = self.existing_draft(
            LineItems=[{"AccountCode": "WRONG", "TaxType": "NONE", "LineAmount": 180.0}]
        )
        result = capture(arguments())
        self.assertFalse(result.values["completed"])
        self.assertEqual(result.values["returned_for"], "existing_draft_differs")
        self.assertIn("account", result.values["detail"])
        self.assertNotEqual(self.xero.bills[invoice_id]["Status"], "AUTHORISED")

    def test_a_draft_with_a_changed_tax_type_is_never_authorised(self) -> None:
        capture, _ = self.existing_draft(
            LineItems=[{"AccountCode": "310", "TaxType": "INPUT3", "LineAmount": 180.0}]
        )
        result = capture(arguments())
        self.assertFalse(result.values["completed"])
        self.assertIn("tax type", result.values["detail"])

    def test_a_draft_with_extra_lines_is_never_authorised(self) -> None:
        capture, _ = self.existing_draft(
            LineItems=[
                {"AccountCode": "310", "TaxType": "NONE", "LineAmount": 90.0},
                {"AccountCode": "310", "TaxType": "NONE", "LineAmount": 90.0},
            ]
        )
        result = capture(arguments())
        self.assertFalse(result.values["completed"])
        self.assertIn("line(s)", result.values["detail"])

    def test_every_stated_field_must_match_before_resuming(self) -> None:
        """Review finding: only account, tax type and amount were compared.

        A draft with the wrong dates, currency, reference, line amount type,
        description and tax amount was authorised because its total matched.
        """
        line = {
            "AccountCode": "310",
            "TaxType": "NONE",
            "LineAmount": 180.0,
            "Description": "Electronic components",
            "TaxAmount": 0.0,
        }
        for label, change in (
            ("date", {"Date": "1999-01-01"}),
            ("due date", {"DueDate": "1999-01-01"}),
            ("reference", {"Reference": "wrong reference"}),
            ("line amount type", {"LineAmountTypes": "Inclusive"}),
            ("description", {"LineItems": [{**line, "Description": "Wrong purchase"}]}),
            ("tax amount", {"LineItems": [{**line, "TaxAmount": 99.0}]}),
        ):
            with self.subTest(altered=label):
                capture, invoice_id = self.existing_draft(**change)
                result = capture(arguments())
                self.assertFalse(
                    result.values["completed"],
                    f"a draft with a changed {label} was authorised",
                )
                self.assertEqual(
                    result.values["returned_for"], "existing_draft_differs"
                )
                self.assertNotEqual(
                    self.xero.bills[invoice_id]["Status"], "AUTHORISED"
                )

    def test_the_whole_draft_is_read_back_not_the_search_projection(self) -> None:
        """A search result carries less than the bill actually holds."""
        from alx.tools.xero import _draft_mismatch

        requested = {
            "InvoiceNumber": "n",
            "Date": "2026-08-20",
            "DueDate": "2026-09-20",
            "CurrencyCode": "ZAR",
            "Reference": "r",
            "LineAmountTypes": "NoTax",
            "Contact": {"ContactID": "c"},
            "LineItems": [
                {
                    "AccountCode": "310",
                    "TaxType": "NONE",
                    "Quantity": 1,
                    "UnitAmount": 180,
                    "Description": "d",
                    "TaxAmount": 0,
                }
            ],
        }
        stored = {
            **requested,
            "Status": "DRAFT",
            "Total": 180.0,
            "LineItems": [
                {
                    "AccountCode": "310",
                    "TaxType": "NONE",
                    "LineAmount": 180.0,
                    "Quantity": 1,
                    "UnitAmount": 180,
                    "Description": "d",
                    "TaxAmount": 0.0,
                }
            ],
        }
        self.assertEqual(_draft_mismatch(stored, requested), "")
        self.assertIn(
            "currency", _draft_mismatch({**stored, "CurrencyCode": "USD"}, requested)
        )
        self.assertIn(
            "different supplier",
            _draft_mismatch({**stored, "Contact": {"ContactID": "other"}}, requested),
        )
        self.assertIn("could not be read back", _draft_mismatch(None, requested))

    def test_the_comparison_never_fails_open(self) -> None:
        """Review finding: a bill matching on nothing returned no mismatch.

        Dates in Xero's serialised form were skipped rather than compared, and
        the fresh status, total, quantity and unit amount were not checked at
        all, so an AUTHORISED bill totalling 999 with 1970 dates passed as
        matching a 180 draft.
        """
        from alx.tools.xero import _draft_mismatch

        requested = {
            "InvoiceNumber": "n",
            "Date": "2026-08-20",
            "DueDate": "2026-09-20",
            "CurrencyCode": "ZAR",
            "Reference": "r",
            "LineAmountTypes": "NoTax",
            "Contact": {"ContactID": "c"},
            "LineItems": [
                {
                    "AccountCode": "310",
                    "TaxType": "NONE",
                    "Quantity": 1,
                    "UnitAmount": 180,
                    "Description": "d",
                    "TaxAmount": 0,
                }
            ],
        }
        matching = {
            **requested,
            "Status": "DRAFT",
            "Total": 180.0,
            "LineItems": [
                {
                    "AccountCode": "310",
                    "TaxType": "NONE",
                    "LineAmount": 180.0,
                    "Quantity": 1,
                    "UnitAmount": 180,
                    "Description": "d",
                    "TaxAmount": 0.0,
                }
            ],
        }
        self.assertEqual(_draft_mismatch(matching, requested), "")

        split = [
            {
                "AccountCode": "310",
                "TaxType": "NONE",
                "LineAmount": 180.0,
                "Quantity": 2,
                "UnitAmount": 90,
                "Description": "d",
                "TaxAmount": 0.0,
            }
        ]
        for label, stored in (
            ("the reviewer's whole case", {
                **matching,
                "Status": "AUTHORISED",
                "Total": 999.00,
                "Date": "/Date(0+0000)/",
                "DueDate": "/Date(0+0000)/",
                "LineItems": split,
            }),
            ("already authorised", {**matching, "Status": "AUTHORISED"}),
            ("no status at all", {**matching, "Status": ""}),
            ("a different total", {**matching, "Total": 999.0}),
            ("the same product from different parts", {**matching, "LineItems": split}),
            ("a date that cannot be read", {**matching, "Date": "rubbish"}),
            ("an empty date", {**matching, "DueDate": ""}),
        ):
            with self.subTest(stored=label):
                self.assertNotEqual(
                    _draft_mismatch(stored, requested),
                    "",
                    f"{label} was reported as matching",
                )

    def test_a_submitted_bill_is_not_this_capability_to_authorise(self) -> None:
        """D-018 requires a DRAFT for authorisation.

        D-019 separately names DRAFT or SUBMITTED for discarding, so the
        distinction is deliberate: a submitted bill awaits someone's approval.
        """
        from alx.tools.xero import _draft_mismatch

        requested = {
            "InvoiceNumber": "n",
            "Date": "2026-08-20",
            "DueDate": "2026-09-20",
            "CurrencyCode": "ZAR",
            "Reference": "r",
            "LineAmountTypes": "NoTax",
            "Contact": {"ContactID": "c"},
            "LineItems": [
                {
                    "AccountCode": "310",
                    "TaxType": "NONE",
                    "Quantity": 1,
                    "UnitAmount": 180,
                    "Description": "d",
                    "TaxAmount": 0,
                }
            ],
        }
        line = {
            "AccountCode": "310",
            "TaxType": "NONE",
            "LineAmount": 180.0,
            "Quantity": 1,
            "UnitAmount": 180,
            "Description": "d",
            "TaxAmount": 0.0,
        }
        draft = {**requested, "Status": "DRAFT", "Total": 180.0, "LineItems": [line]}
        self.assertEqual(_draft_mismatch(draft, requested), "")
        self.assertIn(
            "not a draft", _draft_mismatch({**draft, "Status": "SUBMITTED"}, requested)
        )

    def test_a_line_that_states_no_quantity_cannot_be_verified(self) -> None:
        """Absent evidence is not agreement."""
        from alx.tools.xero import _draft_mismatch

        requested = {
            "InvoiceNumber": "n",
            "Date": "2026-08-20",
            "DueDate": "2026-09-20",
            "CurrencyCode": "ZAR",
            "Reference": "r",
            "LineAmountTypes": "NoTax",
            "Contact": {"ContactID": "c"},
            "LineItems": [
                {
                    "AccountCode": "310",
                    "TaxType": "NONE",
                    "Quantity": 1,
                    "UnitAmount": 180,
                    "Description": "d",
                    "TaxAmount": 0,
                }
            ],
        }
        for missing in ("Quantity", "UnitAmount"):
            with self.subTest(missing=missing):
                line = {
                    "AccountCode": "310",
                    "TaxType": "NONE",
                    "LineAmount": 180.0,
                    "Quantity": 1,
                    "UnitAmount": 180,
                    "Description": "d",
                    "TaxAmount": 0.0,
                }
                line.pop(missing)
                stored = {
                    **requested,
                    "Status": "DRAFT",
                    "Total": 180.0,
                    "LineItems": [line],
                }
                self.assertIn("to verify", _draft_mismatch(stored, requested))

    def test_a_xero_serialised_date_is_compared_not_skipped(self) -> None:
        """Xero returns its own date format; it must be read, not ignored."""
        from alx.contracts import xero_date

        self.assertEqual(xero_date("2026-08-20"), "2026-08-20")
        self.assertEqual(xero_date("/Date(1788134400000+0000)/"), "2026-08-31")
        self.assertEqual(xero_date("/Date(0+0000)/"), "1970-01-01")
        for unreadable in ("rubbish", "", "/Date(nonsense)/"):
            with self.subTest(value=unreadable):
                self.assertIsNone(xero_date(unreadable))

    def test_an_unaltered_draft_still_resumes(self) -> None:
        """Idempotency must survive the stricter check."""
        capture = self.build()
        capture(arguments())
        created = len(self.xero.bills)
        invoice_id = next(iter(self.xero.bills))
        self.xero.bills[invoice_id]["Status"] = "DRAFT"
        result = capture(arguments())
        self.assertTrue(result.values["completed"])
        self.assertEqual(len(self.xero.bills), created, "a retry duplicated the bill")
        self.assertIn("resumed_existing_draft", result.values["steps"])


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
        self.assertIn("no contact is named", result.values["detail"])
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

    def test_the_granular_write_steps_no_longer_exist_at_all(self) -> None:
        """Law 0: withheld is not deleted. The old steps must be gone.

        These five capabilities were once retained but hidden from planning.
        Law 0 forbids a superseded path surviving as recovery-only code, so
        this asserts absence from the definitions, the executors and the
        policies -- not merely absence from the catalogue.
        """
        import tempfile

        from alx.bootstrap.xero import build_xero_runtime

        superseded = (
            "execute_xero_bill",
            "create_xero_draft_bill",
            "update_xero_draft_bill",
            "attach_mail_document_to_xero_bill",
            "authorise_xero_bill",
        )
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_xero_runtime(
                xero_settings(unattended_bill_writes=True),
                Path(directory),
                FakeMail(),
                lambda: "call",
            )
        offered = {item.capability_id for item in runtime.definitions}
        self.assertIn(CAPTURE_SUPPLIER_INVOICE, offered)
        for capability_id in superseded:
            with self.subTest(capability_id=capability_id):
                self.assertNotIn(capability_id, offered)
                self.assertNotIn(capability_id, runtime.executors)
                self.assertNotIn(capability_id, runtime.policies)

    def test_no_second_route_to_a_bill_survives_in_the_registry(self) -> None:
        """The named outcome has exactly one production entry point."""
        from alx.tools import XERO_DEFINITIONS

        effectful = {
            item.capability_id
            for item in XERO_DEFINITIONS
            if item.side_effect is SideEffect.EFFECTFUL
        }
        # Capture commits a bill; delete discards one. Nothing else writes.
        self.assertEqual(
            effectful, {CAPTURE_SUPPLIER_INVOICE, "delete_xero_draft_bill"}
        )

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
