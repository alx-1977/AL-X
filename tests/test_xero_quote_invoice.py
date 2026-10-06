"""D-037: a customer's quote and the PO that accepts it become a DRAFT invoice.

BlueNova sent a purchase order for a quoted amount on 2026-10-06, and AL/X
could not act on it. Which quote the PO accepts and what its number is are
her judgement; these tests prove the decided steps happen exactly, refuse
what does not match, and never leave a second invoice behind.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from alx.bootstrap.xero import (  # noqa: E402
    XERO_QUOTE_INVOICE_PERMISSION,
    XERO_READ_PERMISSION,
    build_xero_runtime,
)
from alx.contracts import CapabilityResultState, MailAttachment  # noqa: E402
from alx.providers.xero import (  # noqa: E402
    ACCOUNTING_URL,
    XeroAccountingAdapter,
    XeroConnection,
)
from alx.tools import (  # noqa: E402
    FIND_XERO_QUOTES,
    INVOICE_XERO_QUOTE,
    build_xero_executors,
)
from support import xero_settings  # noqa: E402

PO_BYTES = b"bluenova-po"
PO_DIGEST = hashlib.sha256(PO_BYTES).hexdigest()
LINES = [
    {"LineItemID": "line-1", "Description": "Sensor boards", "Quantity": 10.0,
     "UnitAmount": 100.0, "AccountCode": "200", "TaxType": "OUTPUT2",
     "LineAmount": 1000.0, "TaxAmount": 150.0},
]


class QuotingXero:
    """Quotes, sales invoices and attachments, as Xero reports them."""

    def __init__(self, status: str = "SENT") -> None:
        self.quote = {
            "QuoteID": "quote-1", "QuoteNumber": "QU-0042", "Status": status,
            "Contact": {"ContactID": "bluenova", "Name": "BlueNova"},
            "DateString": "2026-10-01T00:00:00", "CurrencyCode": "ZAR",
            "LineAmountTypes": "Exclusive", "LineItems": LINES,
            "SubTotal": 1000.0, "TotalTax": 150.0, "Total": 1150.0,
            "Reference": "", "Title": "Sensor boards",
        }
        self.invoices: dict[str, dict] = {}
        self.created: list[dict] = []
        self.status_changes: list[str] = []
        self.attachments: list[tuple[dict, bytes]] = []
        self.totals_drift = False

    def read_quote(self, quote_id):
        return dict(self.quote) if quote_id == "quote-1" else None

    def quotes_for_contact(self, contact_id):
        return (dict(self.quote),) if contact_id == "bluenova" else ()

    def set_quote_status(self, quote_record, status):
        self.status_changes.append(status)
        self.quote = {**self.quote, "Status": status}
        return dict(self.quote)

    def sales_invoices_for_contact(self, contact_id):
        return tuple(
            dict(item) for item in self.invoices.values()
            if item["Contact"]["ContactID"] == contact_id
        )

    def create_draft_sales_invoice(self, invoice):
        self.created.append(invoice)
        invoice_id = f"invoice-{len(self.created)}"
        self.invoices[invoice_id] = {
            **invoice, "InvoiceID": invoice_id, "InvoiceNumber": "INV-0100",
            "SubTotal": 1000.0, "TotalTax": 150.0,
            "Total": 1200.0 if self.totals_drift else 1150.0,
        }
        return dict(self.invoices[invoice_id])

    def read_sales_invoice(self, invoice_id):
        return dict(self.invoices[invoice_id]) if invoice_id in self.invoices else None

    def attach_bill_document(self, invoice_id, filename, media_type, content):
        record = {"AttachmentID": f"att-{len(self.attachments) + 1}",
                  "FileName": filename, "MimeType": media_type}
        self.attachments.append((record, content))
        return record

    def list_bill_attachments(self, _invoice_id):
        return tuple(record for record, _content in self.attachments)

    def read_bill_attachment(self, _invoice_id, attachment_id, _media_type):
        return next(c for r, c in self.attachments if r["AttachmentID"] == attachment_id)


class FakeMail:
    def read_attachment(self, _reference, _attachment_id):
        return (
            MailAttachment("2", "BlueNova-PO-7781.pdf", "application/pdf",
                           len(PO_BYTES), PO_DIGEST, ""),
            PO_BYTES,
        )


def arguments(**changes) -> dict:
    values = {
        "quote_id": "quote-1",
        "po_number": "PO-7781",
        "po_document": {
            "mailbox_id": "INBOX", "uid_validity": "777", "uid": "60500",
            "attachment_id": "2", "expected_sha256": PO_DIGEST,
        },
    }
    values.update(changes)
    return values


class QuoteToInvoiceTests(unittest.TestCase):
    def executors(self, xero):
        return build_xero_executors(
            xero, FakeMail(), lambda: "call-1", today=lambda: date(2026, 10, 6),
        )

    def test_a_sent_quote_becomes_a_verified_draft_invoice(self) -> None:
        xero = QuotingXero()
        result = self.executors(xero)[INVOICE_XERO_QUOTE](arguments())
        values = result.values
        self.assertTrue(values["completed"], values)
        self.assertEqual(xero.status_changes, ["ACCEPTED", "INVOICED"])
        (created,) = xero.created
        self.assertEqual(created["Type"], "ACCREC")
        self.assertEqual(created["Status"], "DRAFT")
        self.assertEqual(created["Reference"], "PO-7781")
        self.assertEqual(created["Contact"], {"ContactID": "bluenova"})
        self.assertEqual(created["Date"], "2026-10-06")
        self.assertEqual(created["CurrencyCode"], "ZAR")
        # Copied field for field, without the quote line's identity or the
        # amounts Xero derives.
        self.assertEqual(created["LineItems"], [{
            "Description": "Sensor boards", "Quantity": 10.0, "UnitAmount": 100.0,
            "AccountCode": "200", "TaxType": "OUTPUT2",
        }])
        self.assertEqual(values["attached"], ("BlueNova-PO-7781.pdf",))
        self.assertEqual(values["steps"], (
            "read_po_document", "read_quote", "accepted_quote",
            "created_draft_invoice", "attached_and_verified_po", "read_back",
            "marked_quote_invoiced",
        ))

    def test_it_never_approves_or_sends(self) -> None:
        xero = QuotingXero()
        self.executors(xero)[INVOICE_XERO_QUOTE](arguments())
        self.assertEqual({item["Status"] for item in xero.invoices.values()}, {"DRAFT"})

    def test_a_draft_or_declined_quote_returns_unposted(self) -> None:
        for status in ("DRAFT", "DECLINED"):
            with self.subTest(status=status):
                xero = QuotingXero(status)
                result = self.executors(xero)[INVOICE_XERO_QUOTE](arguments())
                self.assertFalse(result.values["completed"])
                self.assertEqual(result.values["returned_for"], f"quote_{status.lower()}")
                self.assertEqual(xero.created, [])
                self.assertEqual(xero.status_changes, [])

    def test_an_unposted_return_stops_a_plan(self) -> None:
        """A return awaits her judgement; a plan must not run on as if done."""
        from alx.contracts import ExecutionOutcome

        result = self.executors(QuotingXero("DRAFT"))[INVOICE_XERO_QUOTE](arguments())
        self.assertIs(result.outcome, ExecutionOutcome.AMBIGUOUS)
        done = self.executors(QuotingXero())[INVOICE_XERO_QUOTE](arguments())
        self.assertIsNone(done.outcome)

    def test_a_po_that_does_not_match_its_digest_writes_nothing(self) -> None:
        xero = QuotingXero()
        result = self.executors(xero)[INVOICE_XERO_QUOTE](arguments(po_document={
            **arguments()["po_document"], "expected_sha256": "0" * 64,
        }))
        self.assertEqual(result.failure["code"], "source_mismatch")
        self.assertEqual((xero.created, xero.status_changes), ([], []))

    def test_rerunning_resumes_and_never_invoices_twice(self) -> None:
        xero = QuotingXero()
        execute = self.executors(xero)[INVOICE_XERO_QUOTE]
        execute(arguments())
        again = execute(arguments())
        self.assertTrue(again.values["completed"], again.values)
        self.assertEqual(len(xero.created), 1)
        self.assertEqual(len(xero.attachments), 1)
        self.assertIn("resumed_existing_draft", again.values["steps"])

    def test_an_approved_invoice_for_the_po_returns_unposted(self) -> None:
        xero = QuotingXero("ACCEPTED")
        xero.invoices["invoice-9"] = {
            "InvoiceID": "invoice-9", "InvoiceNumber": "INV-0099",
            "Contact": {"ContactID": "bluenova"}, "Reference": "PO-7781",
            "Status": "AUTHORISED", "Type": "ACCREC",
        }
        result = self.executors(xero)[INVOICE_XERO_QUOTE](arguments())
        self.assertEqual(result.values["returned_for"], "invoice_already_exists")
        self.assertEqual(xero.created, [])

    def test_totals_that_disagree_leave_the_quote_uninvoiced(self) -> None:
        xero = QuotingXero()
        xero.totals_drift = True
        result = self.executors(xero)[INVOICE_XERO_QUOTE](arguments())
        self.assertFalse(result.values["completed"])
        self.assertEqual(result.values["returned_for"], "read_back_mismatch")
        self.assertNotIn("INVOICED", xero.status_changes)

    def test_a_resumed_draft_with_edited_lines_is_not_accepted(self) -> None:
        """Equal totals are not equal lines."""
        xero = QuotingXero("ACCEPTED")
        xero.invoices["invoice-7"] = {
            "InvoiceID": "invoice-7", "InvoiceNumber": "INV-0107", "Type": "ACCREC",
            "Contact": {"ContactID": "bluenova"}, "Reference": "PO-7781",
            "Status": "DRAFT", "CurrencyCode": "ZAR",
            "SubTotal": 1000.0, "TotalTax": 150.0, "Total": 1150.0,
            "LineItems": [{"Description": "Something else", "Quantity": 1.0,
                           "UnitAmount": 1000.0, "AccountCode": "200",
                           "TaxType": "OUTPUT2"}],
        }
        result = self.executors(xero)[INVOICE_XERO_QUOTE](arguments())
        self.assertEqual(result.values["returned_for"], "read_back_mismatch")
        self.assertNotIn("INVOICED", xero.status_changes)

    def test_an_unknown_quote_is_a_declared_failure(self) -> None:
        result = self.executors(QuotingXero())[INVOICE_XERO_QUOTE](
            arguments(quote_id="quote-404")
        )
        self.assertEqual(result.failure["code"], "quote_not_found")

    def test_quotes_are_listed_for_one_customer(self) -> None:
        result = self.executors(QuotingXero())[FIND_XERO_QUOTES]({"contact_id": "bluenova"})
        (quote,) = result.values["quotes"]
        self.assertEqual(quote["quote_number"], "QU-0042")
        self.assertEqual(quote["total"], "1150.0")
        self.assertEqual(quote["date"], "2026-10-01")
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)


class QuoteRuntimeTests(unittest.TestCase):
    def test_invoicing_has_its_own_standing_permission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_xero_runtime(
                xero_settings(), Path(directory), FakeMail(), lambda: "call-1",
            )
        policy = runtime.policies[INVOICE_XERO_QUOTE]
        self.assertEqual(policy.permission_references, frozenset({XERO_QUOTE_INVOICE_PERMISSION}))
        self.assertFalse(policy.approval_required)
        self.assertEqual(
            runtime.policies[FIND_XERO_QUOTES].permission_references,
            frozenset({XERO_READ_PERMISSION}),
        )
        self.assertIn(XERO_QUOTE_INVOICE_PERMISSION, runtime.permissions)


class QuoteAdapterTests(unittest.TestCase):
    class ConnectedOAuth:
        def connection(self):
            return XeroConnection("access-token", "tenant-1")

    def test_a_status_change_carries_the_contact_and_date_xero_requires(self) -> None:
        adapter = XeroAccountingAdapter(self.ConnectedOAuth(), timeout_seconds=17)
        response = Mock(status_code=200)
        response.json.return_value = {"Quotes": [{"QuoteID": "quote-1", "Status": "ACCEPTED"}]}
        with patch("httpx.request", return_value=response) as request:
            adapter.set_quote_status(QuotingXero().quote, "ACCEPTED")
        args, kwargs = request.call_args
        self.assertEqual(args, ("POST", f"{ACCOUNTING_URL}/Quotes"))
        self.assertEqual(kwargs["json"], {"Quotes": [{
            "QuoteID": "quote-1", "Status": "ACCEPTED",
            "Contact": {"ContactID": "bluenova"}, "Date": "2026-10-01",
        }]})


    def test_an_invoice_on_a_later_page_is_still_found(self) -> None:
        """Only the first 100 were read, so a PO invoiced long ago was missed."""
        adapter = XeroAccountingAdapter(self.ConnectedOAuth(), timeout_seconds=17)

        def page(number, count):
            response = Mock(status_code=200)
            response.json.return_value = {"Invoices": [
                {"InvoiceID": f"p{number}-{index}", "Type": "ACCREC", "Status": "PAID",
                 "Reference": "PO-7781" if number == 2 and index == 0 else ""}
                for index in range(count)
            ]}
            return response

        with patch("httpx.request", side_effect=[page(1, 100), page(2, 3)]) as request:
            found = adapter.sales_invoices_for_contact("bluenova")
        self.assertEqual(len(found), 103)
        self.assertIn("PO-7781", {item["Reference"] for item in found})
        self.assertEqual(request.call_count, 2)
        self.assertIn("page=2", request.call_args_list[1].args[1])

    def test_the_sales_invoice_call_cannot_create_a_bill(self) -> None:
        """Bills keep their one path; this call makes only DRAFT sales invoices."""
        from alx.contracts import XeroAccessError

        adapter = XeroAccountingAdapter(self.ConnectedOAuth(), timeout_seconds=17)
        for invoice in (
            {"Type": "ACCPAY", "Status": "DRAFT"},
            {"Type": "ACCREC", "Status": "AUTHORISED"},
        ):
            with self.subTest(invoice=invoice), patch("httpx.request") as request:
                with self.assertRaises(XeroAccessError):
                    adapter.create_draft_sales_invoice(invoice)
                request.assert_not_called()

if __name__ == "__main__":
    unittest.main()
