from __future__ import annotations

import csv
import hashlib
import io
import tempfile
import unittest
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pypdf import PdfWriter  # noqa: E402
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject  # noqa: E402

from alx.contracts import DhlDocumentError, MailAttachment  # noqa: E402
from alx.providers import DhlImportAnalyzerAdapter  # noqa: E402
from alx.providers.dhl import _classify_customs_document  # noqa: E402
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
    def test_committed_sanitized_v1_layouts_parse_to_recorded_values(self) -> None:
        fixture_root = Path(__file__).parent / "fixtures" / "dhl"
        cases = (
            (
                "dfm_20260421",
                "DHL-WAYBILL-8339567983",
                "DFM202604215028901",
                "1116.15",
                {"import_vat": "1100.55", "customs_duty": "15.60"},
            ),
            (
                "dfm_20260719",
                "DHL-WAYBILL-7096903730",
                "DFM202607195025382",
                "207.00",
                {"import_vat": "168.75", "customs_duty": "38.25"},
            ),
        )
        for name, invoice_number, declaration, total, lines in cases:
            with self.subTest(name=name):
                result = DhlImportAnalyzerAdapter().analyze_customs(
                    [
                        (fixture_root / f"worksheet_{name}_sanitized.pdf").read_bytes(),
                        (fixture_root / f"sad500_{name}_sanitized.pdf").read_bytes(),
                    ]
                )
                self.assertTrue(result["verified"])
                self.assertEqual(result["provisional_invoice_number"], invoice_number)
                self.assertEqual(result["declaration"], declaration)
                self.assertEqual(result["total"], total)
                self.assertEqual(
                    {item["category"]: item["amount"] for item in result["lines"]},
                    lines,
                )

    @unittest.skipUnless(
        (Path(__file__).resolve().parents[2] / "JARVIS" / "static").exists(),
        "private sibling JARVIS fixtures are not available",
    )
    def test_every_unique_private_v1_customs_document_still_parses_locally(self) -> None:
        expected = {
            "05645ee75f300b3b81e5be4ce9d1a3ab2eac9a65e996997cfa96535e52f56b8e": ("customs_worksheet", "DFM202604215028901", 4),
            "47989158bd89c9805412a0c586af69af953dd0de722bbd3b9add2c43846218c7": ("sad_500", "DFM202604215028901", 4),
            "7ae9f4cc3a7235db8104b076f8a7bf3c2bb02d14126dfde52abf9f1a2e05e1e6": ("customs_worksheet", "DFM202607195025382", 2),
            "88b95e4ab5a38738831acb8a9646a2d96bec484b0728973104dd2e78ddfa678b": ("sad_500", "DFM202604215028901", 1),
            "bee9260d3efcf8393c5352f0a9ff4565dda246b2093affa4ec654bda6bc4801c": ("sad_500", "DFM202604215028901", 1),
            "cdcfc5254325c1f18020f85dd7a24501146fdb45a9c8b6972ef39c974a9cbe78": ("customs_worksheet", "DFM202604215028901", 1),
            "e1abac65f75377c678132194a63ee683d3675561ca44c0f70c1483b0f542acea": ("sad_500", "DFM202607195025382", 2),
            "f29c1fa1ce8e75db3e16e2e1470261e3a12fb535510cebf180f82461e6adb620": ("customs_worksheet", "DFM202604215028901", 1),
        }
        root = Path(__file__).resolve().parents[2] / "JARVIS" / "static"
        found: dict[str, list[bytes]] = {digest: [] for digest in expected}

        def members(payload: bytes):
            try:
                with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                    for member in archive.infolist():
                        if member.is_dir():
                            continue
                        content = archive.read(member)
                        if member.filename.lower().endswith(".zip"):
                            yield from members(content)
                        elif member.filename.lower().endswith(".pdf"):
                            yield content
            except zipfile.BadZipFile:
                return

        for archive_path in root.glob("*.zip"):
            for payload in members(archive_path.read_bytes()):
                digest = hashlib.sha256(payload).hexdigest()
                if digest in found:
                    found[digest].append(payload)

        for digest, (kind, declaration, count) in expected.items():
            with self.subTest(digest=digest):
                self.assertEqual(len(found[digest]), count)
                for payload in found[digest]:
                    classified_kind, value = _classify_customs_document(payload)
                    self.assertEqual(classified_kind, kind)
                    self.assertEqual(
                        value.declaration if hasattr(value, "declaration") else value,
                        declaration,
                    )

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


class UntrustedDocumentBoundTests(unittest.TestCase):
    """A mail attachment is untrusted input.

    Greptile finding: a document handed over directly, rather than through the
    bounded archive path, was parsed with no byte, row, page or run limit, so
    one large or pathological CSV or PDF could exhaust memory or CPU and take
    the local runtime down.
    """

    def test_an_oversized_csv_is_refused_before_parsing(self) -> None:
        from alx.providers.dhl import _DOCUMENT_BYTES, _parse_invoices

        with self.assertRaises(DhlDocumentError) as captured:
            _parse_invoices(b"x" * (_DOCUMENT_BYTES + 1))
        self.assertEqual(captured.exception.code, "invoice_too_large")

    def test_an_oversized_pdf_is_refused_before_parsing(self) -> None:
        from alx.providers.dhl import _DOCUMENT_BYTES, _runs_by_page

        with self.assertRaises(DhlDocumentError) as captured:
            _runs_by_page(b"%PDF-" + b"x" * _DOCUMENT_BYTES)
        self.assertEqual(captured.exception.code, "worksheet_too_large")

    def test_a_csv_with_too_many_rows_is_refused(self) -> None:
        from alx.providers.dhl import _INVOICE_ROWS, _parse_invoices

        header = "Invoice Number,Line Type\n"
        rows = "".join(f"INV{index},S\n" for index in range(_INVOICE_ROWS + 5))
        with self.assertRaises(DhlDocumentError) as captured:
            _parse_invoices((header + rows).encode())
        self.assertEqual(captured.exception.code, "invoice_too_many_rows")

    def test_too_many_customs_documents_are_refused(self) -> None:
        from alx.providers.dhl import _CUSTOMS_DOCUMENTS

        analyzer = DhlImportAnalyzerAdapter()
        payloads = [b"%PDF-"] * (_CUSTOMS_DOCUMENTS + 1)
        for label, call in (
            ("analyze", lambda: analyzer.analyze_customs(payloads)),
            ("reconcile", lambda: analyzer.reconcile(mybill_csv(), payloads)),
        ):
            with self.subTest(path=label):
                with self.assertRaises(DhlDocumentError) as captured:
                    call()
                self.assertEqual(
                    captured.exception.code, "too_many_customs_documents"
                )

    def test_a_bound_failure_names_the_limit_not_the_document(self) -> None:
        """A refusal must not carry the private document into the error."""
        from alx.providers.dhl import _DOCUMENT_BYTES, _parse_invoices

        with self.assertRaises(DhlDocumentError) as captured:
            _parse_invoices(b"secret-invoice-content" * (_DOCUMENT_BYTES // 8))
        self.assertNotIn("secret", str(captured.exception))
        self.assertIsNone(captured.exception.__cause__)

    def test_a_hostile_page_is_refused_before_the_work_is_done(self) -> None:
        """Greptile finding: the run limit was checked too late.

        pypdf decodes a page's whole content stream before the visitor sees a
        single run, so aborting on the first run of a hostile page still cost
        twelve seconds. Measuring the stream first takes milliseconds.
        """
        import time

        from alx.providers.dhl import _WORKSHEET_RUNS, _runs_by_page

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
        commands = ["BT"] + [
            f"/F1 8 Tf 1 0 0 1 {index % 600} {index % 700} Tm (r{index}) Tj"
            for index in range(_WORKSHEET_RUNS * 3)
        ] + ["ET"]
        stream = DecodedStreamObject()
        stream.set_data("\n".join(commands).encode())
        page[NameObject("/Contents")] = stream
        output = io.BytesIO()
        writer.write(output)

        started = time.monotonic()
        with self.assertRaises(DhlDocumentError) as captured:
            _runs_by_page(output.getvalue())
        elapsed = time.monotonic() - started
        self.assertEqual(captured.exception.code, "worksheet_content_too_large")
        self.assertLess(
            elapsed, 2.0, "the document was parsed before it was refused"
        )

    def test_the_production_extraction_path_is_bounded(self) -> None:
        """Codex finding: the bounds were on the wrong layer.

        The mail adapter extracts a PDF's text before any capability sees it,
        and that extraction was unbounded. Every earlier test used FakeMail, so
        none of them touched the path a real attachment actually takes.
        """
        import zlib

        from pypdf.generic import NumberObject, StreamObject

        from alx.providers.icloud_mail import (
            _DOCUMENT_BYTES,
            _PAGE_STORED_BYTES,
            _attachment_text,
        )

        self.assertEqual(
            _attachment_text("application/pdf", b"%PDF-" + b"x" * _DOCUMENT_BYTES, None),
            "",
            "an oversized PDF must not be extracted",
        )
        # Codex finding: the size limit guarded only PDFs, so a 24MB CSV was
        # decoded and returned whole.
        oversized_csv = b"a,b,c\n" * ((_DOCUMENT_BYTES // 6) + 1)
        self.assertGreater(len(oversized_csv), _DOCUMENT_BYTES)
        self.assertEqual(
            _attachment_text("text/csv", oversized_csv, None),
            "",
            "an oversized text attachment must not be returned",
        )

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
        # A decompression bomb: small on the wire, large once decoded. A check
        # that calls get_contents() cannot catch this, because that call
        # decodes the stream to answer.
        expanded = (
            "BT "
            + " ".join(
                f"/F1 8 Tf 1 0 0 1 {index % 600} {index % 700} Tm (r{index}) Tj"
                for index in range(200_000)
            )
            + " ET"
        ).encode()
        compressed = zlib.compress(expanded)
        stream = StreamObject()
        stream._data = compressed
        stream[NameObject("/Filter")] = NameObject("/FlateDecode")
        stream[NameObject("/Length")] = NumberObject(len(compressed))
        page[NameObject("/Contents")] = writer._add_object(stream)
        output = io.BytesIO()
        writer.write(output)
        hostile = output.getvalue()
        self.assertLess(
            len(hostile), _DOCUMENT_BYTES, "the bomb must be small on the wire"
        )
        self.assertGreater(len(expanded), _PAGE_STORED_BYTES)

        import time
        import tracemalloc

        tracemalloc.start()
        started = time.monotonic()
        extracted = _attachment_text("application/pdf", hostile, None)
        elapsed = time.monotonic() - started
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        self.assertEqual(extracted, "", "a decompression bomb must not be decoded")
        self.assertLess(elapsed, 2.0, "the bomb was decoded before being refused")
        self.assertLess(
            peak, 20_000_000, "the bomb was expanded in memory before refusal"
        )

    def test_a_real_attachment_still_yields_its_text(self) -> None:
        """The bounds must not stop a genuine worksheet being read."""
        from alx.providers.icloud_mail import _attachment_text

        fixture = (
            Path(__file__).parent
            / "fixtures"
            / "dhl"
            / "worksheet_dfm_20260421_sanitized.pdf"
        )
        text = _attachment_text("application/pdf", fixture.read_bytes(), None)
        self.assertIn("CUSTOMS WORKSHEET", text.upper())

    def test_the_real_v1_worksheets_are_well_inside_the_bounds(self) -> None:
        """The limits must not refuse a genuine customs worksheet."""
        from alx.providers.dhl import _parse_worksheet

        for path in sorted(
            (Path(__file__).parent / "fixtures" / "dhl").glob("worksheet_*.pdf")
        ):
            with self.subTest(fixture=path.name):
                worksheet = _parse_worksheet(path.read_bytes())
                self.assertTrue(worksheet.declaration)

    def test_an_ordinary_document_still_parses(self) -> None:
        """The bounds must not refuse real work."""
        result = DhlImportAnalyzerAdapter().reconcile(
            mybill_csv(), [worksheet_pdf(), sad500_pdf()]
        )
        self.assertTrue(result["reconciled"])


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
