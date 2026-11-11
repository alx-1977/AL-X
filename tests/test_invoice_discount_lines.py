"""Discounts and credits become their own negative bill lines.

Greptile invoice GIH8AVKI-0004 (2026-11-07): USD 193.00 of reviews, a startup
discount of 96.50, total 96.50, then a 2.00 credit applied, charged 94.50. The
bill was built as one 193.00 line against the 96.50 total and refused three
times as a source mismatch. Friedl enters such deductions in Xero as lines with
a negative amount, which is what the bill now carries.
"""

from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from alx.contracts import CapabilityResultState  # noqa: E402
from alx.providers.llamaparse import _invoice_fields  # noqa: E402
from alx.specialists.invoice import ANSWER_SCHEMA, checked_invoice  # noqa: E402
from alx.tools import (  # noqa: E402
    build_xero_executors  # noqa: E402,
)
from test_invoice_capture import FakeMail, FakeXero, arguments  # noqa: E402
from xero_actions import (  # noqa: E402
    CAPTURE_INVOICE,
    XERO,
    by_action,
    xero_arguments,
    xero_call,
    xero_executors,
)


def greptile(**overrides) -> dict:
    values = {
        "document_type": "invoice",
        "supplier_name": "Greptile",
        "invoice_number": "GIH8AVKI-0004",
        "invoice_date": "2026-11-07",
        "due_date": "2026-11-07",
        "currency": "USD",
        "subtotal": "193.00",
        "tax_amount": "",
        "total": "96.50",
        "amount_due": "94.50",
        "description": "Greptile Code Review Oct 17–Nov 7, 2026",
        "adjustments": [
            {"description": "Startup discount", "amount": "-96.50"},
            {"description": "Credit applied", "amount": "-2.00"},
        ],
    }
    values.update(overrides)
    return values


class CheckedInvoiceTests(unittest.TestCase):
    def test_the_greptile_invoice_verifies_against_the_amount_due(self) -> None:
        invoice = checked_invoice(greptile())
        self.assertTrue(invoice["verified"], invoice["problems"])
        self.assertEqual(invoice["bill_total"], "94.50")
        self.assertEqual(
            [item["amount"] for item in invoice["adjustments"]], ["-96.50", "-2.00"]
        )

    def test_a_discount_alone_verifies_against_the_total(self) -> None:
        invoice = checked_invoice(greptile(
            amount_due="",
            adjustments=[{"description": "Startup discount", "amount": "-96.50"}],
        ))
        self.assertTrue(invoice["verified"], invoice["problems"])
        self.assertEqual(invoice["bill_total"], "96.50")

    def test_deductions_matching_no_printed_figure_are_reported(self) -> None:
        invoice = checked_invoice(greptile(
            adjustments=[{"description": "Startup discount", "amount": "-90.00"}],
        ))
        self.assertFalse(invoice["verified"])
        self.assertIn("neither the total", "; ".join(invoice["problems"]))

    def test_a_positive_adjustment_is_not_taken_as_a_deduction(self) -> None:
        invoice = checked_invoice(greptile(
            adjustments=[{"description": "Shipping", "amount": "12.00"}],
        ))
        self.assertFalse(invoice["verified"])
        self.assertIn("is not a deduction", "; ".join(invoice["problems"]))

    def test_deductions_need_the_subtotal_they_come_off(self) -> None:
        invoice = checked_invoice(greptile(subtotal=""))
        self.assertFalse(invoice["verified"])
        self.assertIn("adjustments without a subtotal", invoice["problems"])

    def test_an_invoice_without_deductions_is_checked_as_before(self) -> None:
        invoice = checked_invoice(greptile(
            subtotal="180.00", tax_amount="0.00", total="180.00", amount_due="", adjustments=[],
        ))
        self.assertTrue(invoice["verified"])
        self.assertEqual(invoice["bill_total"], "180.00")

    def test_an_unreadable_tax_is_never_taken_as_zero(self) -> None:
        invoice = checked_invoice(greptile(tax_amount="see page 2"))
        self.assertFalse(invoice["verified"])
        self.assertIn("tax amount unreadable", invoice["problems"])

    def test_an_unreadable_amount_due_is_reported(self) -> None:
        invoice = checked_invoice(greptile(amount_due="ninety"))
        self.assertFalse(invoice["verified"])
        self.assertIn("amount due unreadable", invoice["problems"])

    def test_its_own_output_checks_the_same(self) -> None:
        # Capture re-checks the dict after a currency repair.
        first = checked_invoice(greptile())
        self.assertEqual(checked_invoice(first), first)


class ExtractionSchemaTests(unittest.TestCase):
    def test_the_document_reader_is_asked_for_deductions_and_the_amount_due(self) -> None:
        self.assertIn("adjustments", ANSWER_SCHEMA["required"])
        self.assertIn("amount_due", ANSWER_SCHEMA["required"])
        item = ANSWER_SCHEMA["properties"]["adjustments"]["items"]
        self.assertEqual(set(item["required"]), {"description", "amount"})

    def test_llamaparse_values_keep_the_deduction_lines(self) -> None:
        fields = _invoice_fields({
            "total": 96.5, "amount_due": "94.50",
            "adjustments": [{"description": "Credit applied", "amount": -2.0}],
        })
        self.assertEqual(fields["amount_due"], "94.50")
        self.assertEqual(fields["adjustments"], [{"description": "Credit applied", "amount": "-2.0"}])


    def test_a_malformed_deduction_from_the_reader_is_reported_not_dropped(self) -> None:
        # The discount alone matches the total, so dropping the bad credit
        # entry would have passed an incomplete bill.
        for adjustments in (
            [{"description": "Startup discount", "amount": "-96.50"}, "Credit applied -2.00"],
            "Startup discount -96.50",
        ):
            with self.subTest(adjustments=adjustments):
                fields = _invoice_fields({**greptile(amount_due=""), "adjustments": adjustments})
                invoice = checked_invoice(fields)
                self.assertFalse(invoice["verified"])


class CaptureTests(unittest.TestCase):
    def capture(self, **overrides):
        self.xero = FakeXero()
        self.xero.contacts = (
            {"Name": "Greptile", "ContactID": "c-1", "ContactStatus": "ACTIVE"},
        )
        self.xero.history = ()
        self.xero.accounts = ({"Code": "310", "Status": "ACTIVE", "TaxType": "NONE"},)
        fields = checked_invoice(greptile(**overrides))
        return xero_executors(
            self.xero, FakeMail(), lambda: "call-1", lambda *_: fields, "310", "INPUT3",
        )[CAPTURE_INVOICE](arguments())

    def test_the_greptile_bill_carries_each_deduction_as_a_negative_line(self) -> None:
        result = self.capture()
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
        self.assertTrue(result.values["completed"])
        posted = self.xero.bills[result.values["bill"]["invoice_id"]]
        lines = [(line["Description"], Decimal(str(line["UnitAmount"]))) for line in posted["LineItems"]]
        self.assertEqual(lines, [
            ("Greptile Code Review Oct 17–Nov 7, 2026", Decimal("193.00")),
            ("Startup discount", Decimal("-96.50")),
            ("Credit applied", Decimal("-2.00")),
        ])
        self.assertEqual(result.values["bill"]["total"], "94.50")

    def test_a_line_total_mismatch_tells_alx_both_figures(self) -> None:
        # The bill as it was built before: one subtotal line against the total.
        self.xero = FakeXero()
        self.xero.contacts = ({"Name": "Greptile", "ContactID": "c-1", "ContactStatus": "ACTIVE"},)
        self.xero.history = ()
        self.xero.accounts = ({"Code": "310", "Status": "ACTIVE", "TaxType": "NONE"},)
        fields = {**checked_invoice(greptile()), "adjustments": (), "bill_total": ""}
        result = xero_executors(
            self.xero, FakeMail(), lambda: "call-1", lambda *_: fields, "310", "INPUT3",
        )[CAPTURE_INVOICE](arguments())
        self.assertEqual(result.failure["code"], "source_mismatch")
        self.assertIn("193.00", result.failure["service_message"])
        self.assertIn("96.50", result.failure["service_message"])

    def test_deductions_on_a_taxed_invoice_are_returned_not_split(self) -> None:
        result = self.capture(
            subtotal="100.00", tax_amount="15.00", total="105.00", amount_due="",
            adjustments=[{"description": "Discount", "amount": "-10.00"}],
        )
        self.assertFalse(result.values["completed"])
        self.assertEqual(result.values["returned_for"], "tax_allocation_unresolved")
        self.assertEqual(self.xero.bills, {})


if __name__ == "__main__":
    unittest.main()
