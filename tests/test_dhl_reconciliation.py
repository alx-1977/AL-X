from __future__ import annotations

import csv
import hashlib
import io
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pypdf import PdfWriter  # noqa: E402
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject  # noqa: E402

from alx.contracts import DhlDocumentError, MailAttachment  # noqa: E402
from alx.providers import DhlImportAnalyzerAdapter  # noqa: E402
from alx.tools import (  # noqa: E402
    ANALYZE_DHL_CUSTOMS_DOCUMENTS,
    RECONCILE_DHL_IMPORT_DOCUMENTS,
    build_dhl_executors,
)


def mybill_csv(*, vat: str = "1100.55") -> bytes:
    fields = [
        "Invoice Number",
        "Line Type",
        "Invoice Date",
        "Due Date",
        "Currency",
        "Total amount (incl. VAT)",
        "Shipment Number",
        "Declaration/Entry number",
    ]
    for index in range(1, 4):
        fields += [f"XC{index} Code", f"XC{index} Name", f"XC{index} Charge"]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    writer.writerow(
        {
            "Invoice Number": "CPTZR00026033",
            "Line Type": "I",
            "Invoice Date": "20260421",
            "Due Date": "20260428",
            "Currency": "ZAR",
            "Total amount (incl. VAT)": "1316.15",
        }
    )
    writer.writerow(
        {
            "Invoice Number": "CPTZR00026033",
            "Line Type": "S",
            "Shipment Number": "1234567890",
            "Declaration/Entry number": "DFM202604215028901",
            "XC1 Code": "XX",
            "XC1 Name": "IMPORT EXPORT DUTIES",
            "XC1 Charge": "15.60",
            "XC2 Code": "XB",
            "XC2 Name": "IMPORT EXPORT TAXES",
            "XC2 Charge": vat,
            "XC3 Code": "WC",
            "XC3 Name": "DUTY TAX PROCESSING",
            "XC3 Charge": "200.00",
        }
    )
    return buffer.getvalue().encode()


def worksheet_pdf(*, vat: str = "1100.55", total: str = "1116.15") -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    content = f"""BT /F1 10 Tf
1 0 0 1 100 760 Tm (CUSTOMS WORKSHEET) Tj
0 -20 Td (DFM202604215028901) Tj
0 -20 Td (1234567890) Tj
1 0 0 1 458 700 Tm (TOTAL DUTY 15.60) Tj
0 -20 Td (TOTAL VAT {vat}) Tj
1 0 0 1 344 660 Tm (TotalTotal) Tj
1 0 0 1 458 660 Tm (1000.00 {total} 0.00) Tj
ET""".encode()
    stream = DecodedStreamObject()
    stream.set_data(content)
    page[NameObject("/Contents")] = stream
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def sad500_pdf(*, declaration: str = "DFM202604215028901") -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(
        f"BT /F1 10 Tf 1 0 0 1 100 760 Tm (SAD 500 CUSTOMS DECLARATION {declaration}) Tj ET".encode()
    )
    page[NameObject("/Contents")] = stream
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class DhlAnalyzerTests(unittest.TestCase):
    def test_v1_customs_rules_produce_a_balanced_bill_proposal(self) -> None:
        result = DhlImportAnalyzerAdapter().reconcile(
            mybill_csv(), [worksheet_pdf(), sad500_pdf()]
        )
        self.assertTrue(result["reconciled"])
        self.assertEqual(result["invoice_number"], "CPTZR00026033")
        self.assertEqual(result["total"], "1316.15")
        self.assertEqual(
            {line["category"]: line["amount"] for line in result["lines"]},
            {
                "import_vat": "1100.55",
                "customs_duty": "15.60",
                "clearance_fee": "200.00",
            },
        )
        self.assertEqual(result["errors"], ())

    def test_mismatched_vat_refuses_reconciliation(self) -> None:
        result = DhlImportAnalyzerAdapter().reconcile(
            mybill_csv(), [worksheet_pdf(vat="1100.53", total="1116.13"), sad500_pdf()]
        )
        self.assertFalse(result["reconciled"])
        self.assertTrue(any("VAT mismatch" in item for item in result["errors"]))

    def test_unknown_charge_code_is_never_swept_into_an_account(self) -> None:
        payload = mybill_csv().replace(b",WC,DUTY TAX PROCESSING,200.00", b",ZZ,UNKNOWN,200.00")
        result = DhlImportAnalyzerAdapter().reconcile(payload, [worksheet_pdf(), sad500_pdf()])
        self.assertFalse(result["reconciled"])
        self.assertTrue(any("unrecognised charge code" in item for item in result["errors"]))

    def test_invalid_private_document_is_not_retained_on_the_error(self) -> None:
        with self.assertRaises(DhlDocumentError) as captured:
            DhlImportAnalyzerAdapter().reconcile(b"private-not-a-csv", [b"private-pdf"])
        self.assertIsNone(captured.exception.__cause__)
        self.assertIsNone(captured.exception.__context__)
        self.assertNotIn("private", str(captured.exception))


class FakeMail:
    def __init__(self) -> None:
        self.payloads = {
            "invoice": ("invoice.csv", "text/csv", mybill_csv()),
            "worksheet": ("worksheet.pdf", "application/pdf", worksheet_pdf()),
            "sad": ("sad500.pdf", "application/pdf", sad500_pdf()),
        }

    def read_attachment(self, _reference, attachment_id):
        filename, media_type, payload = self.payloads[attachment_id]
        digest = hashlib.sha256(payload).hexdigest()
        return (
            MailAttachment(
                attachment_id, filename, media_type, len(payload), digest, ""
            ),
            payload,
        )


class DhlPrimitiveTests(unittest.TestCase):
    def test_result_carries_transitive_mail_provenance(self) -> None:
        mail = FakeMail()

        def source(attachment_id: str, uid: str) -> dict:
            payload = mail.payloads[attachment_id][2]
            return {
                "mailbox_id": "INBOX",
                "uid_validity": "777",
                "uid": uid,
                "attachment_id": attachment_id,
                "expected_sha256": hashlib.sha256(payload).hexdigest(),
            }

        executor = build_dhl_executors(
            mail,
            DhlImportAnalyzerAdapter(),
            lambda: "call-1",
            clock=lambda: datetime(2026, 8, 31, tzinfo=UTC),
        )[RECONCILE_DHL_IMPORT_DOCUMENTS]
        result = executor(
            {
                "invoice_document": source("invoice", "10"),
                "customs_documents": [source("worksheet", "11"), source("sad", "12")],
            }
        )
        self.assertTrue(result.values["reconciled"])
        self.assertIsNotNone(result.provenance)
        self.assertEqual(
            {(item.uid_validity, item.uid) for item in result.provenance.mail_references},
            {("777", "10"), ("777", "11"), ("777", "12")},
        )

    def test_customs_first_stage_produces_verified_provisional_bill(self) -> None:
        mail = FakeMail()

        def source(attachment_id: str, uid: str) -> dict:
            payload = mail.payloads[attachment_id][2]
            return {
                "mailbox_id": "INBOX", "uid_validity": "777", "uid": uid,
                "attachment_id": attachment_id,
                "expected_sha256": hashlib.sha256(payload).hexdigest(),
            }

        executor = build_dhl_executors(mail, DhlImportAnalyzerAdapter(), lambda: "call-1")[ANALYZE_DHL_CUSTOMS_DOCUMENTS]
        result = executor({"customs_documents": [source("worksheet", "11"), source("sad", "12")]})
        self.assertTrue(result.values["verified"])
        self.assertEqual(result.values["provisional_invoice_number"], "DHL-WAYBILL-1234567890")
        self.assertEqual({item["kind"] for item in result.values["documents"]}, {"customs_worksheet", "sad_500"})

    def test_missing_sad500_is_refused(self) -> None:
        with self.assertRaises(DhlDocumentError) as captured:
            DhlImportAnalyzerAdapter().analyze_customs([worksheet_pdf()])
        self.assertEqual(captured.exception.code, "customs_evidence_ambiguous")

    def test_hash_mismatch_stops_before_analysis(self) -> None:
        mail = FakeMail()
        executor = build_dhl_executors(
            mail, DhlImportAnalyzerAdapter(), lambda: "call-1"
        )[RECONCILE_DHL_IMPORT_DOCUMENTS]
        worksheet_payload = mail.payloads["worksheet"][2]
        result = executor(
            {
                "invoice_document": {
                    "mailbox_id": "INBOX",
                    "uid_validity": "777",
                    "uid": "10",
                    "attachment_id": "invoice",
                    "expected_sha256": "wrong",
                },
                "customs_documents": [
                    {
                        "mailbox_id": "INBOX",
                        "uid_validity": "777",
                        "uid": "11",
                        "attachment_id": "worksheet",
                        "expected_sha256": hashlib.sha256(
                            worksheet_payload
                        ).hexdigest(),
                    }
                ],
            }
        )
        self.assertEqual(result.failure["code"], "source_mismatch")


if __name__ == "__main__":
    unittest.main()
