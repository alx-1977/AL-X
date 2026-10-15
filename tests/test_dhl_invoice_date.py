"""A DHL invoice whose date comes out before its label (2026-10-15).

On MyBill invoice CPTZR00032240 the text came out as "2026-10-15 ... INVOICE
DATE": the value was printed to the right of its label on the same line, but
read first. The date is now found by position.
"""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.providers.dhl import parse_invoice_pdf  # noqa: E402


def invoice(runs: list[tuple[float, float, str]]) -> bytes:
    """A one-page PDF drawing each (x, y, text) in the order given."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    stream = DecodedStreamObject()
    stream.set_data(b"".join(
        f"BT /F1 9 Tf 1 0 0 1 {x} {y} Tm ({text}) Tj ET\n".encode() for x, y, text in runs))
    page[NameObject("/Contents")] = stream
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


COMMON = [
    (29, 700, "DHL International (Pty) Ltd"),
    (397, 765, "CPTZR00032240"),
    (29, 600, "HAWB NO. 1575091475"),
    (29, 300, "NET AMOUNT PAYABLE 4,388.15"),
]


class InvoiceDateTests(unittest.TestCase):
    def test_a_date_read_before_its_label_is_still_its_date(self) -> None:
        # The value drawn first, as the real invoice's text order has it.
        parsed = parse_invoice_pdf(invoice([
            (470, 745.5, "2026-10-15"), (397, 745.5, "INVOICE DATE"),
            (397, 725.5, "DUE DATE"), (470, 725.5, "2026-10-22"), *COMMON]))
        self.assertEqual((parsed["invoice_date"], parsed["due_date"]),
                         ("2026-10-15", "2026-10-22"))

    def test_a_date_just_below_its_label_is_found(self) -> None:
        parsed = parse_invoice_pdf(invoice([
            (397, 745.5, "INVOICE DATE"), (397, 732, "15/10/2026"), *COMMON]))
        self.assertEqual(parsed["invoice_date"], "2026-10-15")

    def test_a_date_beside_another_label_is_not_taken(self) -> None:
        parsed = parse_invoice_pdf(invoice([
            (397, 725.5, "DUE DATE"), (470, 725.5, "2026-10-22"),
            (397, 600, "INVOICE DATE"), *COMMON]))
        self.assertEqual(parsed["invoice_date"], "")


if __name__ == "__main__":
    unittest.main()
