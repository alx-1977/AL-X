from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
import zipfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pypdf import PdfWriter  # noqa: E402
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject  # noqa: E402

from alx.contracts import (  # noqa: E402
    CapabilityResultState,
    DhlDocumentError,
    MailAttachment,
    XeroAccessError,
)
from alx.providers import DhlImportAnalyzerAdapter  # noqa: E402
from alx.providers.dhl import _classify_customs_document  # noqa: E402
from alx.tools import (  # noqa: E402
    PROCESS_DHL_IMPORT,
    build_dhl_executors,
)


def worksheet_pdf(
    *,
    vat: str = "1100.55",
    total: str = "1116.15",
    waybill: str = "1234567890",
) -> bytes:
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
0 -20 Td ({waybill}) Tj
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
    def test_duty_tax_csv_classifies_and_reconciles_to_the_cent(self) -> None:
        payload = (FIXTURES / "mybill_cptir_sanitized.csv").read_bytes()
        analyzer = DhlImportAnalyzerAdapter()
        self.assertEqual(analyzer.classify(payload), "dhl_duty_tax_invoice")
        evidence = analyzer.invoice_evidence(payload)
        self.assertTrue(evidence["verified"])
        self.assertEqual(evidence["invoice_number"], "CPTIR00273840")
        self.assertEqual(evidence["waybill"], "1921099471")
        self.assertEqual(evidence["total"], "508.76")
        self.assertEqual(
            [line["amount"] for line in evidence["lines"]],
            ["136.55", "22.21", "350.00"],
        )

    def test_an_unknown_charge_is_not_misclassified_as_duty_tax_paid(self) -> None:
        payload = (FIXTURES / "mybill_cptir_sanitized.csv").read_bytes().replace(
            b"DUTY TAX PAID", b"UNRELATED CHARGE"
        )
        self.assertEqual(DhlImportAnalyzerAdapter().classify(payload), "unrecognised")

    def test_customs_and_freight_shapes_are_separated_by_document_evidence(self) -> None:
        analyzer = DhlImportAnalyzerAdapter()
        self.assertEqual(
            analyzer.classify((FIXTURES / "mybill_customs_sanitized.csv").read_bytes()),
            "dhl_customs_invoice",
        )
        self.assertEqual(
            analyzer.classify((FIXTURES / "mybill_freight_sanitized.csv").read_bytes()),
            "dhl_freight_invoice",
        )

    @unittest.skipUnless(
        (Path(__file__).resolve().parents[2] / "JARVIS" / "tests" / "fixtures" / "dhl").exists(),
        "private sibling V1 invoice fixtures are not available",
    )
    def test_all_private_v1_mybill_invoices_classify_by_their_evidence(self) -> None:
        root = Path(__file__).resolve().parents[2] / "JARVIS" / "tests" / "fixtures" / "dhl"
        expected = {
            "CPTR001005873_gdb.csv": "dhl_freight_invoice",
            "CPTZR00026033_gdb.csv": "dhl_customs_invoice",
            "CPTZR00028679_gdb.csv": "dhl_customs_invoice",
        }
        analyzer = DhlImportAnalyzerAdapter()
        for filename, kind in expected.items():
            with self.subTest(filename=filename):
                self.assertEqual(analyzer.classify((root / filename).read_bytes()), kind)

    def test_an_explicit_zero_net_charge_never_falls_back_to_gross(self) -> None:
        payload = (FIXTURES / "mybill_cptir_sanitized.csv").read_bytes().replace(
            b"136.55,0.00,0.00,136.55", b"136.55,0.00,-136.55,0.00"
        )
        evidence = DhlImportAnalyzerAdapter().invoice_evidence(payload)
        self.assertNotIn("136.55", [line["amount"] for line in evidence["lines"]])
        self.assertFalse(evidence["verified"])

    def test_line_tax_is_detected_even_when_total_tax_claims_zero(self) -> None:
        payload = (FIXTURES / "mybill_cptir_sanitized.csv").read_bytes().replace(
            b"136.55,0.00,0.00,136.55", b"136.55,1.00,0.00,136.55"
        )
        evidence = DhlImportAnalyzerAdapter().invoice_evidence(payload)
        self.assertTrue(evidence["tax_present"])

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
                result = DhlImportAnalyzerAdapter().customs_evidence(
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

    def test_v1_customs_rules_produce_balanced_customs_evidence(self) -> None:
        result = DhlImportAnalyzerAdapter().customs_evidence(
            [worksheet_pdf(), sad500_pdf()]
        )
        self.assertTrue(result["verified"])
        self.assertEqual(result["duty"], "15.60")
        self.assertEqual(result["vat"], "1100.55")
        self.assertEqual(result["total"], "1116.15")
        self.assertEqual(result["waybill"], "1234567890")
        self.assertEqual(result["errors"], ())

    def test_the_committed_invoice_fixture_parses_to_recorded_values(self) -> None:
        """The sanitized equivalent of the live CPTIR00273840 invoice.

        Committed under tests/fixtures/dhl/ alongside the worksheet and SAD 500
        layouts, in the same Crystal Reports geometry, and asserted here so the
        invoice parser has real layout coverage rather than only a synthetic
        document built for one test.
        """
        fields = DhlImportAnalyzerAdapter().invoice_fields(
            (FIXTURES / "invoice_cptir_sanitized.pdf").read_bytes()
        )
        self.assertEqual(fields["invoice_number"], "CPTIR00273840")
        self.assertEqual(fields["waybill"], "1921099471")
        self.assertEqual(fields["total"], "508.76")
        self.assertEqual(fields["invoice_date"], "2026-08-31")
        self.assertEqual(fields["due_date"], "2026-09-07")

    def test_a_south_african_date_format_is_read_not_dropped(self) -> None:
        """DHL SA states DD/MM/YYYY; reading only ISO would lose every date."""
        from alx.providers.dhl import _labelled_date

        self.assertEqual(_labelled_date("DUE DATE 07/09/2026", "DUE DATE"), "2026-09-07")
        self.assertEqual(_labelled_date("DUE DATE 2026-09-07", "DUE DATE"), "2026-09-07")
        self.assertEqual(_labelled_date("DUE DATE 32/13/2026", "DUE DATE"), "")

    def test_the_assessment_date_comes_from_the_declaration(self) -> None:
        """Stated by the identifier, never guessed from the arrival date."""
        result = DhlImportAnalyzerAdapter().customs_evidence(
            [worksheet_pdf(), sad500_pdf()]
        )
        self.assertEqual(result["assessed_on"], "2026-04-21")

    def test_a_declaration_without_a_real_date_yields_none(self) -> None:
        from alx.providers.dhl import _assessed_on

        for declaration in ("DFM209913015028901", "SHORT", "ABCXXXXXXXXXXXXXX"):
            with self.subTest(declaration=declaration):
                self.assertEqual(_assessed_on(declaration), "")

    def test_a_worksheet_that_does_not_balance_is_refused(self) -> None:
        result = DhlImportAnalyzerAdapter().customs_evidence(
            [worksheet_pdf(vat="1100.53"), sad500_pdf()]
        )
        self.assertFalse(result["verified"])
        self.assertTrue(any("does not balance" in item for item in result["problems"]))

    def test_a_sad500_for_another_declaration_is_refused(self) -> None:
        result = DhlImportAnalyzerAdapter().customs_evidence(
            [worksheet_pdf(), sad500_pdf(declaration="DFM202607195025382")]
        )
        self.assertFalse(result["verified"])
        self.assertTrue(any("declaration" in item for item in result["problems"]))

    def test_invalid_private_document_is_not_retained_on_the_error(self) -> None:
        with self.assertRaises(DhlDocumentError) as captured:
            DhlImportAnalyzerAdapter().customs_evidence([b"private-pdf"])
        self.assertIsNone(captured.exception.__cause__)


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "dhl"


def invoice_pdf(
    *,
    invoice_date: str | None = None,
    waybill: str | None = None,
    total: str | None = None,
) -> bytes:
    """The committed sanitized MyBill invoice.

    Built by `tests/fixtures/dhl/build_sanitized_fixtures.py` in the real
    Crystal Reports layout and committed alongside the worksheet and SAD 500
    fixtures. The keyword arguments rewrite one stated value in place so a
    test can exercise a missing date or another shipment without inventing a
    different layout.
    """
    payload = (FIXTURES / "invoice_cptir_sanitized.pdf").read_bytes()
    if invoice_date is not None:
        # Same byte length, so the PDF stream stays valid: a shorter value is
        # padded with spaces rather than shifting every offset after it.
        original = b"INVOICE DATE 31/08/2026"
        replacement = f"INVOICE DATE {invoice_date}".encode()
        payload = payload.replace(original, replacement.ljust(len(original)))
    if waybill is not None:
        payload = payload.replace(b"1921099471", waybill.encode())
    if total is not None:
        # The committed fixture pads this field so a longer total can be
        # substituted without changing the PDF stream length.
        original = b"NET AMOUNT PAYABLE 508.76" + b" " * 4
        replacement = f"NET AMOUNT PAYABLE {total}".encode()
        if len(replacement) > len(original):
            raise ValueError("total too long for the fixture field")
        payload = payload.replace(original, replacement.ljust(len(original)))
    return payload


class FakeMail:
    def __init__(self) -> None:
        self.payloads = {
            "invoice": (
                "invoice.pdf",
                "application/pdf",
                # Same shipment as the worksheet fixture: its duty and VAT
                # come to 1116.15, so 1616.15 leaves 500.00 of clearance.
                invoice_pdf(waybill="1234567890", total="1616.15"),
            ),
            "worksheet": ("worksheet.pdf", "application/pdf", worksheet_pdf()),
            "sad": ("sad500.pdf", "application/pdf", sad500_pdf()),
            "duty_csv": (
                "CPTIR00273840.csv",
                "text/csv",
                (FIXTURES / "mybill_cptir_sanitized.csv").read_bytes(),
            ),
            "duty_pdf": (
                "CPTIR00273840.pdf",
                "application/pdf",
                invoice_pdf(),
            ),
            "customs_csv": (
                "customs-invoice.csv",
                "text/csv",
                b"Line Type,Invoice Number,Shipment Number,Invoice Date,Due Date,Currency,Total amount (incl. VAT),Total Tax,Weight Charge,Declaration/Entry number,XC1 Code,XC1 Name,XC1 Charge,XC1 Tax,XC1 Total\n"
                b"S,CPTIR00273840,1234567890,20260824,20260907,ZAR,1616.15,0.00,0,DFM202604215028901,XX,IMPORT EXPORT DUTIES,1616.15,0.00,1616.15\n",
            ),
            "freight_csv": (
                "freight.csv",
                "text/csv",
                (FIXTURES / "mybill_freight_sanitized.csv").read_bytes(),
            ),
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

    def test_an_oversized_invoice_is_refused_before_parsing(self) -> None:
        from alx.providers.dhl import _DOCUMENT_BYTES, parse_invoice_pdf

        with self.assertRaises(DhlDocumentError) as captured:
            parse_invoice_pdf(b"%PDF-" + b"x" * _DOCUMENT_BYTES)
        self.assertEqual(captured.exception.code, "worksheet_too_large")

    def test_an_oversized_pdf_is_refused_before_parsing(self) -> None:
        from alx.providers.dhl import _DOCUMENT_BYTES, _runs_by_page

        with self.assertRaises(DhlDocumentError) as captured:
            _runs_by_page(b"%PDF-" + b"x" * _DOCUMENT_BYTES)
        self.assertEqual(captured.exception.code, "worksheet_too_large")

    def test_too_many_customs_documents_are_refused(self) -> None:
        from alx.providers.dhl import _CUSTOMS_DOCUMENTS

        analyzer = DhlImportAnalyzerAdapter()
        payloads = [b"%PDF-"] * (_CUSTOMS_DOCUMENTS + 1)
        with self.assertRaises(DhlDocumentError) as captured:
            analyzer.customs_evidence(payloads)
        self.assertEqual(captured.exception.code, "too_many_customs_documents")

    def test_a_bound_failure_names_the_limit_not_the_document(self) -> None:
        """A refusal must not carry the private document into the error."""
        from alx.providers.dhl import _DOCUMENT_BYTES, parse_invoice_pdf

        with self.assertRaises(DhlDocumentError) as captured:
            parse_invoice_pdf(b"%PDF-secret-invoice" * (_DOCUMENT_BYTES // 8))
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
        from alx.providers.dhl import _runs_by_page

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
        # Repetition gives this stream an extreme expansion ratio: unlike the
        # earlier test, it remains far below the stored-byte ceiling and would
        # therefore reach pypdf unless the decoder itself is bounded.
        expanded = b"q\n" * (12 * 1024 * 1024)
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
        self.assertLess(
            len(compressed), _PAGE_STORED_BYTES,
            "the bomb must pass the stored-byte preflight",
        )

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

        started = time.monotonic()
        with self.assertRaises(DhlDocumentError) as captured:
            _runs_by_page(hostile)
        self.assertEqual(captured.exception.code, "worksheet_content_too_large")
        self.assertLess(
            time.monotonic() - started,
            2.0,
            "the DHL parser decoded the bomb before refusing it",
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
        evidence = DhlImportAnalyzerAdapter().customs_evidence(
            [worksheet_pdf(), sad500_pdf()]
        )
        self.assertTrue(evidence["verified"])
        self.assertEqual(
            DhlImportAnalyzerAdapter().invoice_fields(invoice_pdf())["waybill"],
            "1921099471",
        )


class FakeXeroBills:
    """Enough of Xero to follow one bill through both stages."""

    def __init__(self, state: dict | None = None) -> None:
        # `state` lets a second, independently constructed adapter observe the
        # same Xero organisation, which is what survives a process restart.
        shared = state if state is not None else {}
        self.state = shared
        self.bills: dict[str, dict] = shared.setdefault("bills", {})
        self.by_number: dict[str, str] = shared.setdefault("by_number", {})
        self.attachments: dict[str, list] = shared.setdefault("attachments", {})
        self.authorised: list[str] = shared.setdefault("authorised", [])
        self.created = 0

    def search_contacts(self, _term):
        return (
            {
                "ContactID": "contact-1",
                "Name": "DHL EXPRESS",
                "ContactStatus": "ACTIVE",
            },
            {
                "ContactID": "dhl-contact",
                "Name": "DHL EXPRESS SOUTH AFRICA",
                "ContactStatus": "ACTIVE",
            },
        )

    def list_accounts(self):
        return (
            {"Code": "820", "Status": "ACTIVE", "TaxType": "NONE"},
            {"Code": "426", "Status": "ACTIVE", "TaxType": "NONE"},
            {"Code": "425", "Status": "ACTIVE", "TaxType": "NONE"},
        )

    def find_bill(self, invoice_number, _contact_id=""):
        invoice_id = self.by_number.get(invoice_number)
        return dict(self.bills[invoice_id]) if invoice_id else None

    def read_bill(self, invoice_id):
        bill = self.bills.get(invoice_id)
        return dict(bill) if bill else None

    def _total(self, lines):
        total = Decimal("0")
        for line in lines:
            total += Decimal(str(line["Quantity"])) * Decimal(str(line["UnitAmount"]))
        return format(total, "f")

    def create_draft_bill(self, bill):
        self.created += 1
        self.state["created"] = self.state.get("created", 0) + 1
        invoice_id = f"bill-{self.state['created']}"
        stored = {
            **bill,
            "InvoiceID": invoice_id,
            "Status": "DRAFT",
            "Total": self._total(bill["LineItems"]),
            "HasAttachments": False,
        }
        self.bills[invoice_id] = stored
        self.by_number[bill["InvoiceNumber"]] = invoice_id
        return dict(stored)

    def update_draft_bill(self, invoice_id, bill):
        previous = self.bills[invoice_id]
        self.by_number.pop(previous["InvoiceNumber"], None)
        stored = {
            **previous,
            **bill,
            "InvoiceID": invoice_id,
            "Total": self._total(bill["LineItems"]),
        }
        self.bills[invoice_id] = stored
        self.by_number[stored["InvoiceNumber"]] = invoice_id
        return dict(stored)

    def attach_bill_document(self, invoice_id, filename, media_type, content):
        records = self.attachments.setdefault(invoice_id, [])
        record = {
            "AttachmentID": f"attachment-{len(records) + 1}",
            "FileName": filename,
            "MimeType": media_type,
        }
        records.append((record, content))
        self.bills[invoice_id] = {**self.bills[invoice_id], "HasAttachments": True}
        return record

    def list_bill_attachments(self, invoice_id):
        return tuple(record for record, _ in self.attachments.get(invoice_id, ()))

    def read_bill_attachment(self, invoice_id, attachment_id, _media_type):
        return next(
            content
            for record, content in self.attachments.get(invoice_id, ())
            if record["AttachmentID"] == attachment_id
        )

    def authorise_bill(self, invoice_id):
        self.authorised.append(invoice_id)
        self.bills[invoice_id] = {**self.bills[invoice_id], "Status": "AUTHORISED"}
        return dict(self.bills[invoice_id])


def source_for(mail: "FakeMail", attachment_id: str, uid: str) -> dict:
    payload = mail.payloads[attachment_id][2]
    return {
        "mailbox_id": "INBOX",
        "uid_validity": "777",
        "uid": uid,
        "attachment_id": attachment_id,
        "expected_sha256": hashlib.sha256(payload).hexdigest(),
    }


def executor_for(mail, xero, supplier_name: str = "DHL EXPRESS"):
    return build_dhl_executors(
        mail,
        DhlImportAnalyzerAdapter(),
        xero,
        lambda: "call-1",
        "820",
        "426",
        "425",
        supplier_name,
        clock=lambda: datetime(2026, 8, 31, tzinfo=UTC),
    )[PROCESS_DHL_IMPORT]


class DhlImportLifecycleTests(unittest.TestCase):
    """One capability, two stages, one evolving bill.

    The customs documents arrive first and draft a provisional bill for the
    duty and VAT SARS assessed. The invoice arrives days later and completes
    that same bill in place. The stage is chosen by the documents themselves.
    """

    def setUp(self) -> None:
        self.mail = FakeMail()
        self.xero = FakeXeroBills()
        self.executor = executor_for(self.mail, self.xero)

    def customs(self, executor=None):
        return (executor or self.executor)(
            {
                "documents": [
                    source_for(self.mail, "worksheet", "11"),
                    source_for(self.mail, "sad", "12"),
                ]
            }
        )

    def invoice(self, executor=None):
        return (executor or self.executor)(
            {"documents": [source_for(self.mail, "invoice", "10")]}
        )

    def duty_tax(self, executor=None):
        return (executor or self.executor)(
            {
                "documents": [
                    source_for(self.mail, "duty_csv", "20"),
                    source_for(self.mail, "duty_pdf", "20"),
                ]
            }
        )

    def test_duty_tax_paid_posts_one_verified_bill_without_a_customs_draft(self) -> None:
        result = self.duty_tax()
        self.assertTrue(result.values["completed"])
        self.assertEqual(result.values["stage"], "dhl_duty_tax_invoice")
        self.assertEqual(self.xero.created, 1)
        bill = result.values["bill"]
        self.assertEqual(bill["Status"], "AUTHORISED")
        self.assertEqual(bill["InvoiceNumber"], "CPTIR00273840")
        self.assertEqual(Decimal(bill["Total"]), Decimal("508.76"))
        self.assertEqual(
            [line["AccountCode"] for line in bill["LineItems"]],
            ["426", "426", "426"],
        )
        self.assertTrue(all(line["TaxAmount"] == 0 for line in bill["LineItems"]))
        self.assertEqual(result.values["attached"], ("CPTIR00273840.pdf",))

    def test_duty_tax_paid_is_retry_safe_after_authorisation_failure(self) -> None:
        original = self.xero.authorise_bill
        attempts = 0

        def fail_once(invoice_id):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise XeroAccessError("connection_failed")
            return original(invoice_id)

        self.xero.authorise_bill = fail_once
        first = self.duty_tax()
        self.assertEqual(first.failure["code"], "connection_failed")
        second = self.duty_tax()
        self.assertTrue(second.values["completed"])
        self.assertEqual(self.xero.created, 1)

    def test_duty_tax_paid_is_retry_safe_after_attachment_failure(self) -> None:
        original = self.xero.attach_bill_document
        attempts = 0

        def fail_once(invoice_id, filename, media_type, content):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise XeroAccessError("connection_failed")
            return original(invoice_id, filename, media_type, content)

        self.xero.attach_bill_document = fail_once
        first = self.duty_tax()
        self.assertEqual(first.failure["code"], "connection_failed")
        second = self.duty_tax()
        self.assertTrue(second.values["completed"])
        self.assertEqual(self.xero.created, 1)

    def test_a_draft_changed_during_attachment_is_not_authorised(self) -> None:
        original = self.xero.attach_bill_document

        def attach_then_tamper(invoice_id, filename, media_type, content):
            record = original(invoice_id, filename, media_type, content)
            changed = dict(self.xero.bills[invoice_id])
            changed["CurrencyCode"] = "USD"
            self.xero.bills[invoice_id] = changed
            return record

        self.xero.attach_bill_document = attach_then_tamper
        result = self.duty_tax()
        self.assertFalse(result.values["completed"])
        self.assertEqual(result.values["returned_for"], "draft_changed")
        self.assertEqual(self.xero.authorised, [])

    def test_a_changed_reference_during_attachment_is_not_authorised(self) -> None:
        original = self.xero.attach_bill_document

        def attach_then_tamper(invoice_id, filename, media_type, content):
            record = original(invoice_id, filename, media_type, content)
            changed = dict(self.xero.bills[invoice_id])
            changed["Reference"] = "unrelated shipment"
            self.xero.bills[invoice_id] = changed
            return record

        self.xero.attach_bill_document = attach_then_tamper
        result = self.duty_tax()
        self.assertEqual(result.values["returned_for"], "draft_changed")
        self.assertEqual(self.xero.authorised, [])

    def test_duty_tax_csv_and_pdf_must_identify_the_same_invoice(self) -> None:
        filename, media_type, payload = self.mail.payloads["duty_pdf"]
        self.mail.payloads["duty_pdf"] = (
            filename,
            media_type,
            payload.replace(b"1921099471", b"9999999999"),
        )
        result = self.duty_tax()
        self.assertFalse(result.values["completed"])
        self.assertEqual(result.values["returned_for"], "supporting_document_mismatch")
        self.assertEqual(self.xero.created, 0)

    def test_an_absent_pdf_invoice_date_still_posts(self) -> None:
        """Absence is not disagreement.

        The live refusal: the CSV stated 2026-08-31 and the PDF stated nothing,
        because the parser returns an empty invoice date by design rather than
        read the bare DATE label, which on a DHL invoice matches the due date.
        Comparing that empty value with `!=` read absence as conflict and
        refused a correct invoice before any bill was written.
        """
        filename, media_type, payload = self.mail.payloads["duty_pdf"]
        self.mail.payloads["duty_pdf"] = (
            filename,
            media_type,
            invoice_pdf(invoice_date=""),
        )
        result = self.duty_tax()
        self.assertTrue(result.values["completed"], result.values.get("detail"))
        self.assertEqual(self.xero.created, 1)
        self.assertEqual(self.xero.authorised, ["bill-1"])
        # The CSV is authoritative, so the bill carries its date.
        self.assertEqual(self.xero.bills["bill-1"]["Date"], "2026-08-31")

    def test_a_matching_pdf_invoice_date_posts(self) -> None:
        filename, media_type, payload = self.mail.payloads["duty_pdf"]
        self.mail.payloads["duty_pdf"] = (
            filename,
            media_type,
            invoice_pdf(invoice_date="31/08/2026"),
        )
        result = self.duty_tax()
        self.assertTrue(result.values["completed"], result.values.get("detail"))
        self.assertEqual(self.xero.bills["bill-1"]["Date"], "2026-08-31")

    def test_a_conflicting_pdf_invoice_date_is_refused_before_writing(self) -> None:
        """A real disagreement must still stop the bill."""
        filename, media_type, payload = self.mail.payloads["duty_pdf"]
        self.mail.payloads["duty_pdf"] = (
            filename,
            media_type,
            invoice_pdf(invoice_date="01/01/2020"),
        )
        result = self.duty_tax()
        self.assertFalse(result.values["completed"])
        self.assertEqual(result.values["returned_for"], "supporting_document_mismatch")
        self.assertIn("invoice date", result.values["detail"])
        self.assertEqual(self.xero.created, 0)
        self.assertEqual(self.xero.authorised, [])

    def test_a_missing_csv_invoice_date_is_refused_before_writing(self) -> None:
        """The authoritative date is the CSV's; without it nothing is posted.

        The analyzer refuses an absent date outright, so this never reaches a
        structured return. Either way no bill exists, which is what matters.
        """
        filename, media_type, payload = self.mail.payloads["duty_csv"]
        self.mail.payloads["duty_csv"] = (
            filename,
            media_type,
            payload.replace(b",1921099471,20260831,", b",1921099471,,"),
        )
        result = self.duty_tax()
        self.assertEqual(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure["code"], "invoice_date_missing")
        self.assertEqual(self.xero.created, 0)
        self.assertEqual(self.xero.authorised, [])

    def test_an_unusable_csv_invoice_date_is_refused_before_writing(self) -> None:
        """A date the accounting system cannot use must not reach a write.

        The analyzer normalises what it can parse. This guards the remainder:
        a value that survives parsing but is not a date Xero accepts refuses
        rather than posting a bill under it.
        """
        from alx.providers import DhlImportAnalyzerAdapter

        class UndatedAnalyzer(DhlImportAnalyzerAdapter):
            def invoice_evidence(self, payload):
                return {**super().invoice_evidence(payload), "invoice_date": ""}

        executor = build_dhl_executors(
            self.mail,
            UndatedAnalyzer(),
            self.xero,
            lambda: "call-1",
            "820",
            "426",
            "425",
            "DHL EXPRESS",
            clock=lambda: datetime(2026, 8, 31, tzinfo=UTC),
        )[PROCESS_DHL_IMPORT]
        result = executor(
            {
                "documents": [
                    source_for(self.mail, "duty_csv", "20"),
                    source_for(self.mail, "duty_pdf", "20"),
                ]
            }
        )
        self.assertFalse(result.values["completed"])
        self.assertEqual(result.values["returned_for"], "invoice_date_missing")
        self.assertEqual(self.xero.created, 0)
        self.assertEqual(self.xero.authorised, [])

    def test_the_sanitized_cptir_invoice_posts_end_to_end(self) -> None:
        """Sanitized fixtures reproducing the document that failed in production.

        These are the committed CSV and PDF, not the live attachments: they
        carry the same invoice number, waybill, dates, total and charge lines
        as the real CPTIR00273840, so the shape and values the parser sees
        match. That the live documents themselves post is unproven here.
        """
        result = self.duty_tax()
        self.assertTrue(result.values["completed"], result.values.get("detail"))
        self.assertEqual(result.values["stage"], "dhl_duty_tax_invoice")
        bill = self.xero.bills["bill-1"]
        self.assertEqual(bill["InvoiceNumber"], "CPTIR00273840")
        self.assertEqual(bill["Date"], "2026-08-31")
        self.assertEqual(bill["DueDate"], "2026-09-07")
        self.assertEqual(Decimal(bill["Total"]), Decimal("508.76"))
        self.assertEqual(
            [line["AccountCode"] for line in bill["LineItems"]], ["426", "426", "426"]
        )

    def test_a_duty_tax_invoice_stating_tax_is_refused_before_writing(self) -> None:
        filename, media_type, payload = self.mail.payloads["duty_csv"]
        self.mail.payloads["duty_csv"] = (
            filename,
            media_type,
            payload.replace(b",508.76,0.00,0,", b",508.76,1.00,0,"),
        )
        result = self.duty_tax()
        self.assertFalse(result.values["completed"])
        self.assertEqual(result.values["returned_for"], "invoice_tax_present")
        self.assertEqual(self.xero.created, 0)

    def test_customs_csv_and_pdf_complete_the_existing_customs_draft(self) -> None:
        self.customs()
        result = self.executor(
            {
                "documents": [
                    source_for(self.mail, "customs_csv", "30"),
                    source_for(self.mail, "invoice", "30"),
                ]
            }
        )
        self.assertTrue(result.values["completed"])
        self.assertEqual(result.values["stage"], "dhl_invoice")
        self.assertEqual(self.xero.created, 1)

    def test_freight_is_recognised_and_returned_without_a_xero_write(self) -> None:
        self.xero.list_accounts = lambda: (_ for _ in ()).throw(
            AssertionError("freight classification must not touch Xero")
        )
        result = self.executor(
            {"documents": [source_for(self.mail, "freight_csv", "40")]}
        )
        self.assertFalse(result.values["completed"])
        self.assertEqual(result.values["stage"], "dhl_freight_invoice")
        self.assertEqual(result.values["returned_for"], "freight_not_authorised")
        self.assertEqual(self.xero.created, 0)

    def test_customs_documents_draft_a_provisional_bill(self) -> None:
        result = self.customs()
        self.assertTrue(result.values["completed"])
        self.assertEqual(result.values["stage"], "customs_documents")
        self.assertEqual(result.values["waybill"], "1234567890")
        bill = result.values["bill"]
        self.assertEqual(bill["InvoiceNumber"], "DHL-WAYBILL-1234567890")
        self.assertEqual(bill["Status"], "DRAFT")
        self.assertEqual(Decimal(bill["Total"]), Decimal("1116.15"))

    def test_the_provisional_bill_is_dated_from_the_declaration(self) -> None:
        """Xero requires a date; an empty one must never be sent."""
        self.customs()
        stored = self.xero.bills["bill-1"]
        self.assertEqual(stored["Date"], "2026-04-21")
        self.assertEqual(stored["DueDate"], "2026-04-21")

    def test_the_two_customs_amounts_post_to_their_own_accounts(self) -> None:
        """Import VAT is claimable and duty is not, so they never merge."""
        self.customs()
        lines = self.xero.bills["bill-1"]["LineItems"]
        posted = {
            line["AccountCode"]: Decimal(str(line["UnitAmount"])) for line in lines
        }
        self.assertEqual(posted, {"820": Decimal("1100.55"), "426": Decimal("15.60")})
        for line in lines:
            self.assertEqual(line["TaxAmount"], 0.0)

    def test_the_invoice_completes_the_same_bill_rather_than_a_second_one(self) -> None:
        self.customs()
        result = self.invoice()
        self.assertTrue(result.values["completed"])
        self.assertEqual(result.values["stage"], "dhl_invoice")
        self.assertEqual(self.xero.state["created"], 1)
        self.assertEqual(self.xero.authorised, ["bill-1"])
        bill = result.values["bill"]
        self.assertEqual(bill["InvoiceID"], "bill-1")
        self.assertEqual(bill["InvoiceNumber"], "CPTIR00273840")
        self.assertEqual(bill["Status"], "AUTHORISED")

    def test_the_completed_bill_uses_the_invoices_own_date(self) -> None:
        """The due date is never substituted for a missing invoice date."""
        self.customs()
        self.invoice()
        stored = self.xero.bills["bill-1"]
        self.assertEqual(stored["Date"], "2026-08-31")
        self.assertEqual(stored["DueDate"], "2026-09-07")

    def test_an_invoice_stating_no_date_is_refused(self) -> None:
        """V1's date-guessing defect must not return."""
        self.customs()
        self.mail.payloads["invoice"] = (
            "invoice.pdf",
            "application/pdf",
            invoice_pdf(invoice_date=""),
        )
        result = self.invoice()
        self.assertFalse(result.values["completed"])
        self.assertEqual(result.values["returned_for"], "invoice_date_missing")
        self.assertEqual(self.xero.bills["bill-1"]["Status"], "DRAFT")

    def test_clearance_is_derived_by_subtraction_not_parsed(self) -> None:
        """Invoice total less the duty and VAT already verified."""
        self.customs()
        self.invoice()
        lines = self.xero.bills["bill-1"]["LineItems"]
        clearance = [line for line in lines if line["AccountCode"] == "425"]
        self.assertEqual(len(clearance), 1)
        self.assertEqual(Decimal(str(clearance[0]["UnitAmount"])), Decimal("500.00"))
        self.assertEqual(
            Decimal(self.xero.bills["bill-1"]["Total"]), Decimal("1616.15")
        )

    def test_an_invoice_with_no_customs_stage_returns_to_alx(self) -> None:
        """An export has no customs stage; a second bill is not AL/X's to guess."""
        result = self.invoice()
        self.assertFalse(result.values["completed"])
        self.assertEqual(result.values["returned_for"], "no_matching_draft")
        self.assertEqual(self.xero.state.get("created", 0), 0)

    def test_the_stage_follows_the_documents_not_any_wording(self) -> None:
        """Nothing in the call says which stage to run."""
        customs = self.customs()
        invoice = self.invoice()
        self.assertEqual(customs.values["stage"], "customs_documents")
        self.assertEqual(invoice.values["stage"], "dhl_invoice")

    def test_customs_evidence_and_an_invoice_together_are_refused(self) -> None:
        result = self.executor(
            {
                "documents": [
                    source_for(self.mail, "worksheet", "11"),
                    source_for(self.mail, "sad", "12"),
                    source_for(self.mail, "invoice", "10"),
                ]
            }
        )
        self.assertFalse(result.values["completed"])
        self.assertEqual(result.values["returned_for"], "documents_ambiguous")
        self.assertEqual(self.xero.state.get("created", 0), 0)

    def test_every_source_document_is_attached_and_verified(self) -> None:
        self.customs()
        self.invoice()
        stored = {record["FileName"] for record, _ in self.xero.attachments["bill-1"]}
        self.assertEqual(stored, {"worksheet.pdf", "sad500.pdf", "invoice.pdf"})

    def test_result_carries_transitive_mail_provenance(self) -> None:
        result = self.customs()
        self.assertIsNotNone(result.provenance)
        self.assertEqual(
            {
                (item.uid_validity, item.uid)
                for item in result.provenance.mail_references
            },
            {("777", "11"), ("777", "12")},
        )

    def test_hash_mismatch_stops_before_analysis(self) -> None:
        result = self.executor(
            {
                "documents": [
                    {
                        "mailbox_id": "INBOX",
                        "uid_validity": "777",
                        "uid": "11",
                        "attachment_id": "worksheet",
                        "expected_sha256": "wrong",
                    }
                ]
            }
        )
        self.assertEqual(result.failure["code"], "source_mismatch")
        self.assertEqual(self.xero.state.get("created", 0), 0)

    def test_missing_sad500_is_refused(self) -> None:
        with self.assertRaises(DhlDocumentError) as captured:
            DhlImportAnalyzerAdapter().customs_evidence([worksheet_pdf()])
        self.assertEqual(captured.exception.code, "customs_evidence_ambiguous")

    def test_a_worksheet_alone_never_reaches_xero(self) -> None:
        result = self.executor(
            {"documents": [source_for(self.mail, "worksheet", "11")]}
        )
        self.assertEqual(result.state, CapabilityResultState.FAILED)
        self.assertEqual(self.xero.state.get("created", 0), 0)


class ConfiguredSupplierAndAccountsTests(unittest.TestCase):
    """D-021: the supplier and accounts are configuration, and are validated."""

    def setUp(self) -> None:
        self.mail = FakeMail()
        self.xero = FakeXeroBills()

    def documents(self):
        return {
            "documents": [
                source_for(self.mail, "worksheet", "11"),
                source_for(self.mail, "sad", "12"),
            ]
        }

    def test_the_capability_takes_no_contact_argument(self) -> None:
        """A wrong supplier cannot be supplied to it."""
        from alx.tools.dhl import DEFINITION

        self.assertNotIn("contact_id", DEFINITION.input_schema.properties)
        self.assertEqual(set(DEFINITION.input_schema.properties), {"documents"})

    def test_an_unconfigured_contact_refuses_rather_than_posting(self) -> None:
        executor = executor_for(self.mail, self.xero, supplier_name="")
        result = executor(self.documents())
        self.assertEqual(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure["code"], "dhl_supplier_not_configured")
        self.assertEqual(self.xero.state.get("created", 0), 0)

    def test_the_configured_contact_is_the_one_written(self) -> None:
        executor = executor_for(self.mail, self.xero, supplier_name="DHL EXPRESS SOUTH AFRICA")
        executor(self.documents())
        self.assertEqual(
            self.xero.bills["bill-1"]["Contact"]["ContactID"], "dhl-contact"
        )

    def test_a_contact_absent_from_the_organisation_refuses(self) -> None:
        """D-021: a non-empty contact id is not proof it exists here."""
        executor = executor_for(self.mail, self.xero, supplier_name="Not A Real Supplier")
        result = executor(self.documents())
        self.assertEqual(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure["code"], "contact_not_found")
        self.assertEqual(self.xero.state.get("created", 0), 0)

    def test_an_archived_contact_refuses(self) -> None:
        self.xero.search_contacts = lambda _term: (
            {
                "ContactID": "contact-1",
                "Name": "DHL EXPRESS",
                "ContactStatus": "ARCHIVED",
            },
        )
        executor = executor_for(self.mail, self.xero)
        result = executor(self.documents())
        self.assertEqual(result.failure["code"], "contact_not_found")
        self.assertEqual(self.xero.state.get("created", 0), 0)

    def test_the_contact_is_verified_before_any_write(self) -> None:
        """The check must happen before Xero is asked to create anything."""
        writes = []
        self.xero.create_draft_bill = lambda bill: writes.append(bill)
        executor = executor_for(self.mail, self.xero, supplier_name="Not A Real Supplier")
        executor(self.documents())
        self.assertEqual(writes, [])

    def test_an_account_absent_from_the_organisation_refuses(self) -> None:
        """The same live validation capture_supplier_invoice applies."""
        self.xero.list_accounts = lambda: (
            {"Code": "820", "Status": "ACTIVE"},
            {"Code": "426", "Status": "ACTIVE"},
        )
        executor = executor_for(self.mail, self.xero)
        result = executor(self.documents())
        self.assertEqual(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure["code"], "account_mapping_invalid")
        self.assertEqual(self.xero.state.get("created", 0), 0)

    def test_an_archived_account_is_not_accepted(self) -> None:
        self.xero.list_accounts = lambda: (
            {"Code": "820", "Status": "ACTIVE"},
            {"Code": "426", "Status": "ARCHIVED"},
            {"Code": "425", "Status": "ACTIVE"},
        )
        executor = executor_for(self.mail, self.xero)
        result = executor(self.documents())
        self.assertEqual(result.failure["code"], "account_mapping_invalid")

    def test_import_export_fees_account_must_default_to_no_tax(self) -> None:
        self.xero.list_accounts = lambda: (
            {"Code": "820", "Status": "ACTIVE", "TaxType": "NONE"},
            {"Code": "426", "Status": "ACTIVE", "TaxType": "INPUT3"},
            {"Code": "425", "Status": "ACTIVE", "TaxType": "NONE"},
        )
        result = executor_for(self.mail, self.xero)(self.documents())
        self.assertEqual(result.failure["code"], "account_mapping_invalid")
        self.assertEqual(self.xero.state.get("created", 0), 0)


class TamperedDraftTests(unittest.TestCase):
    """A resumed draft is verified against its evidence, never assumed.

    Each of these was a reproduced defect: a draft edited elsewhere was
    accepted because only its status and total were checked.
    """

    def setUp(self) -> None:
        self.mail = FakeMail()
        self.xero = FakeXeroBills()
        self.executor = executor_for(self.mail, self.xero)
        self.customs_documents = {
            "documents": [
                source_for(self.mail, "worksheet", "11"),
                source_for(self.mail, "sad", "12"),
            ]
        }
        self.executor(self.customs_documents)

    def tamper(self, **changes):
        self.xero.bills["bill-1"] = {**self.xero.bills["bill-1"], **changes}

    def test_a_draft_moved_to_another_supplier_is_refused(self) -> None:
        self.tamper(Contact={"ContactID": "SOMEONE-ELSE", "Name": "Not DHL"})
        result = self.executor(self.customs_documents)
        self.assertFalse(result.values["completed"])
        self.assertEqual(result.values["returned_for"], "draft_changed")
        self.assertIn("contact", result.values["detail"])

    def test_a_draft_switched_to_another_currency_is_refused(self) -> None:
        self.tamper(CurrencyCode="USD")
        result = self.executor(self.customs_documents)
        self.assertEqual(result.values["returned_for"], "draft_changed")
        self.assertIn("USD", result.values["detail"])

    def test_a_draft_recoded_to_another_account_is_refused(self) -> None:
        self.tamper(
            LineItems=[
                {
                    "Description": "Consulting",
                    "Quantity": 1,
                    "UnitAmount": 1116.15,
                    "AccountCode": "999",
                    "TaxAmount": 0.0,
                }
            ]
        )
        result = self.executor(self.customs_documents)
        self.assertEqual(result.values["returned_for"], "draft_changed")
        self.assertIn("999", result.values["detail"])

    def test_a_matching_total_does_not_excuse_wrong_lines(self) -> None:
        """The defect exactly: the total agreed, so everything else passed."""
        self.tamper(
            LineItems=[
                {
                    "Description": "Import VAT",
                    "Quantity": 1,
                    "UnitAmount": 1000.00,
                    "AccountCode": "820",
                    "TaxAmount": 0.0,
                },
                {
                    "Description": "Customs duty",
                    "Quantity": 1,
                    "UnitAmount": 116.15,
                    "AccountCode": "426",
                    "TaxAmount": 0.0,
                },
            ]
        )
        result = self.executor(self.customs_documents)
        self.assertEqual(result.values["returned_for"], "draft_changed")

    def test_an_untampered_draft_still_resumes(self) -> None:
        """The verification must not refuse legitimate work."""
        result = self.executor(self.customs_documents)
        self.assertTrue(result.values["completed"])
        self.assertIn("resumed_verified_draft", result.values["steps"])
        self.assertEqual(self.xero.state["created"], 1)


class CustomsEvidenceOnTheBillTests(unittest.TestCase):
    """The invoice stage re-reads the evidence; it never trusts line items."""

    def setUp(self) -> None:
        self.mail = FakeMail()
        self.xero = FakeXeroBills()
        self.executor = executor_for(self.mail, self.xero)
        self.executor(
            {
                "documents": [
                    source_for(self.mail, "worksheet", "11"),
                    source_for(self.mail, "sad", "12"),
                ]
            }
        )

    def invoice(self):
        return self.executor(
            {"documents": [source_for(self.mail, "invoice", "10")]}
        )

    def test_a_bill_whose_evidence_was_deleted_is_never_authorised(self) -> None:
        """The reproduced defect: authorised carrying only invoice.pdf."""
        self.xero.attachments["bill-1"] = []
        result = self.invoice()
        self.assertFalse(result.values["completed"])
        self.assertEqual(result.values["returned_for"], "customs_evidence_missing")
        self.assertEqual(self.xero.bills["bill-1"]["Status"], "DRAFT")
        self.assertEqual(self.xero.authorised, [])

    def test_losing_only_the_sad500_is_still_refused(self) -> None:
        self.xero.attachments["bill-1"] = [
            record
            for record in self.xero.attachments["bill-1"]
            if record[0]["FileName"] != "sad500.pdf"
        ]
        result = self.invoice()
        self.assertFalse(result.values["completed"])
        self.assertEqual(self.xero.authorised, [])

    def test_evidence_for_another_waybill_is_refused(self) -> None:
        """The stored documents must be this shipment's own."""
        self.xero.attachments["bill-1"] = [
            (
                {
                    "AttachmentID": "a-1",
                    "FileName": "worksheet.pdf",
                    "MimeType": "application/pdf",
                },
                worksheet_pdf(waybill="9999999999"),
            ),
            (
                {
                    "AttachmentID": "a-2",
                    "FileName": "sad500.pdf",
                    "MimeType": "application/pdf",
                },
                sad500_pdf(),
            ),
        ]
        result = self.invoice()
        self.assertFalse(result.values["completed"])
        self.assertIn(
            result.values["returned_for"],
            ("customs_evidence_mismatch", "customs_evidence_missing"),
        )
        self.assertEqual(self.xero.authorised, [])

    def test_the_figures_come_from_the_documents_not_the_lines(self) -> None:
        """Rewriting the lines cannot change what the bill is completed with."""
        self.xero.bills["bill-1"] = {
            **self.xero.bills["bill-1"],
            "LineItems": [
                {
                    "Description": "Import VAT",
                    "Quantity": 1,
                    "UnitAmount": 900.00,
                    "AccountCode": "820",
                    "TaxAmount": 0.0,
                },
                {
                    "Description": "Customs duty",
                    "Quantity": 1,
                    "UnitAmount": 216.15,
                    "AccountCode": "426",
                    "TaxAmount": 0.0,
                },
            ],
        }
        result = self.invoice()
        self.assertFalse(result.values["completed"])
        self.assertEqual(result.values["returned_for"], "draft_changed")


class PartialFailureRecoveryTests(unittest.TestCase):
    """An interrupted import must stay recoverable, never stranded."""

    def setUp(self) -> None:
        self.mail = FakeMail()
        self.xero = FakeXeroBills()
        self.executor = executor_for(self.mail, self.xero)

    def customs(self):
        return self.executor(
            {
                "documents": [
                    source_for(self.mail, "worksheet", "11"),
                    source_for(self.mail, "sad", "12"),
                ]
            }
        )

    def invoice(self):
        return self.executor(
            {"documents": [source_for(self.mail, "invoice", "10")]}
        )

    def test_a_failed_attachment_leaves_the_bill_findable(self) -> None:
        """The reproduced defect: renaming first stranded the bill."""
        self.customs()
        original = self.xero.attach_bill_document

        def refuse(*_args, **_kwargs):
            raise RuntimeError("Xero attachment upload failed")

        self.xero.attach_bill_document = refuse
        with self.assertRaises(RuntimeError):
            self.invoice()
        # Still answering to the provisional number the next run searches for.
        self.assertEqual(
            self.xero.bills["bill-1"]["InvoiceNumber"], "DHL-WAYBILL-1234567890"
        )
        self.assertEqual(self.xero.bills["bill-1"]["Status"], "DRAFT")

        self.xero.attach_bill_document = original
        result = self.invoice()
        self.assertTrue(result.values["completed"])
        self.assertEqual(self.xero.bills["bill-1"]["Status"], "AUTHORISED")
        self.assertEqual(self.xero.state["created"], 1)

    def test_a_failed_authorisation_recovers_and_finishes_the_same_bill(self) -> None:
        """The renamed bill must not be stranded.

        Authorisation failing after the rename leaves a bill that no longer
        answers to its provisional number. A later run finds it by the invoice
        number it now carries and completes it, rather than abandoning it or
        creating a second one.
        """
        self.customs()
        original = self.xero.authorise_bill

        def refuse(*_args, **_kwargs):
            raise RuntimeError("Xero rejected the authorisation")

        self.xero.authorise_bill = refuse
        with self.assertRaises(RuntimeError):
            self.invoice()
        self.assertEqual(
            self.xero.bills["bill-1"]["InvoiceNumber"], "CPTIR00273840"
        )
        self.assertEqual(self.xero.bills["bill-1"]["Status"], "DRAFT")

        self.xero.authorise_bill = original
        result = self.invoice()
        self.assertTrue(result.values["completed"])
        self.assertIn("recovered_renamed_draft", result.values["steps"])
        self.assertEqual(self.xero.bills["bill-1"]["Status"], "AUTHORISED")
        self.assertEqual(self.xero.state["created"], 1)
        # Recovery must not double the clearance line or the total.
        self.assertEqual(
            Decimal(self.xero.bills["bill-1"]["Total"]), Decimal("1616.15")
        )
        self.assertEqual(
            [line["AccountCode"] for line in self.xero.bills["bill-1"]["LineItems"]],
            ["820", "426", "425"],
        )

    def test_an_authorised_import_is_never_processed_twice(self) -> None:
        self.customs()
        self.invoice()
        again = self.invoice()
        self.assertFalse(again.values["completed"])
        self.assertEqual(again.values["returned_for"], "no_matching_draft")
        self.assertEqual(self.xero.authorised, ["bill-1"])
        self.assertEqual(self.xero.state["created"], 1)


# What the invoice stage sends for the fixture shipment: import VAT, customs
# duty and the derived clearance, each NoTax.
EXPECTED_LINES = (
    {
        "Description": "Import VAT (claimable, per SAD 500) — waybill 1234567890",
        "Quantity": 1.0,
        "UnitAmount": 1100.55,
        "AccountCode": "820",
        "TaxAmount": 0.0,
    },
    {
        "Description": "Customs duty (not claimable) — waybill 1234567890",
        "Quantity": 1.0,
        "UnitAmount": 15.60,
        "AccountCode": "426",
        "TaxAmount": 0.0,
    },
    {
        "Description": "DHL clearance and processing — waybill 1234567890",
        "Quantity": 1.0,
        "UnitAmount": 500.00,
        "AccountCode": "425",
        "TaxAmount": 0.0,
    },
)

# The same per-account LineAmount, with every other field replaced. This is
# what a comparison that sums by account cannot see.
CORRUPTED_LINES = (
    {
        "Description": "",
        "Quantity": 99,
        "UnitAmount": 11.1163,
        "AccountCode": "820",
        "TaxAmount": 150.0,
        "LineAmount": 1100.55,
    },
    {
        "Description": "junk",
        "Quantity": 0,
        "UnitAmount": 0,
        "AccountCode": "426",
        "TaxAmount": 2.0,
        "LineAmount": 15.60,
    },
    {
        "Description": "x",
        "Quantity": -5,
        "UnitAmount": -100,
        "AccountCode": "425",
        "TaxAmount": 75.0,
        "LineAmount": 500.00,
    },
)


class StoredUpdateVerificationTests(unittest.TestCase):
    """Xero accepting an update is not proof of what it stored.

    The bill is read back fresh and compared field by field before the
    irreversible authorisation. Previously only the returned InvoiceID was
    checked, so an accepted request that stored a different supplier,
    currency, date or coding still reached authorisation.
    """

    def setUp(self) -> None:
        self.mail = FakeMail()
        self.xero = FakeXeroBills()
        self.executor = executor_for(self.mail, self.xero)
        self.executor(
            {
                "documents": [
                    source_for(self.mail, "worksheet", "11"),
                    source_for(self.mail, "sad", "12"),
                ]
            }
        )

    def store_instead(self, **changes):
        """Xero accepts the update but stores something else."""
        original = self.xero.update_draft_bill

        def crooked(invoice_id, bill):
            original(invoice_id, bill)
            self.xero.bills[invoice_id] = {
                **self.xero.bills[invoice_id],
                **changes,
            }
            return dict(self.xero.bills[invoice_id])

        self.xero.update_draft_bill = crooked
        return self.executor(
            {"documents": [source_for(self.mail, "invoice", "10")]}
        )

    def assert_refused(self, result) -> None:
        self.last_detail = result.values["detail"]
        self.assertFalse(result.values["completed"])
        self.assertEqual(result.values["returned_for"], "update_not_stored")
        self.assertEqual(self.xero.bills["bill-1"]["Status"], "DRAFT")
        self.assertEqual(self.xero.authorised, [])

    def test_a_stored_change_of_supplier_is_caught(self) -> None:
        self.assert_refused(
            self.store_instead(Contact={"ContactID": "WRONG", "Name": "Not DHL"})
        )

    def test_a_stored_change_of_currency_is_caught(self) -> None:
        self.assert_refused(self.store_instead(CurrencyCode="USD"))

    def test_a_stored_change_of_invoice_date_is_caught(self) -> None:
        self.assert_refused(self.store_instead(Date="1999-01-01"))

    def test_a_stored_change_of_due_date_is_caught(self) -> None:
        self.assert_refused(self.store_instead(DueDate="1999-01-01"))

    def test_a_stored_change_of_invoice_number_is_caught(self) -> None:
        self.assert_refused(self.store_instead(InvoiceNumber="SOMETHING-ELSE"))

    def test_a_stored_change_of_coding_is_caught(self) -> None:
        self.assert_refused(
            self.store_instead(
                LineItems=[
                    {
                        "Description": "Whatever",
                        "Quantity": 1,
                        "UnitAmount": 1616.15,
                        "AccountCode": "999",
                        "TaxAmount": 0.0,
                    }
                ]
            )
        )

    def test_a_stored_change_of_the_clearance_amount_is_caught(self) -> None:
        """The total still agrees, so only the line comparison catches it."""
        self.assert_refused(
            self.store_instead(
                LineItems=[
                    {
                        "Description": "Import VAT",
                        "Quantity": 1,
                        "UnitAmount": 1100.55,
                        "AccountCode": "820",
                        "TaxAmount": 0.0,
                    },
                    {
                        "Description": "Customs duty",
                        "Quantity": 1,
                        "UnitAmount": 15.60,
                        "AccountCode": "426",
                        "TaxAmount": 0.0,
                    },
                    {
                        "Description": "Clearance",
                        "Quantity": 1,
                        "UnitAmount": 400.00,
                        "AccountCode": "425",
                        "TaxAmount": 0.0,
                    },
                ],
                Total="1516.15",
            )
        )

    def test_a_blank_currency_is_a_difference_not_an_absence(self) -> None:
        """Missing is not acceptable: the comparison fails closed."""
        self.assert_refused(self.store_instead(CurrencyCode=""))
        self.assertIn("currency", self.last_detail)

    def test_an_absent_currency_key_is_caught(self) -> None:
        self.assert_refused(self.store_instead(CurrencyCode=None))

    def test_a_changed_line_amount_type_is_caught(self) -> None:
        """Inclusive would make Xero read these amounts as tax-bearing.

        No number changes, so only comparing LineAmountTypes catches it.
        """
        self.assert_refused(self.store_instead(LineAmountTypes="Inclusive"))
        self.assertIn("line amount type", self.last_detail)

    def test_an_absent_line_amount_type_is_caught(self) -> None:
        self.assert_refused(self.store_instead(LineAmountTypes=""))

    def test_corrupted_line_fields_are_caught_despite_matching_amounts(
        self,
    ) -> None:
        """Per-account totals agreed; every per-line field was replaced.

        Summing amounts by account made descriptions, quantities, unit amounts
        and tax amounts invisible to the comparison.
        """
        self.assert_refused(self.store_instead(LineItems=CORRUPTED_LINES))

    def test_a_dropped_line_is_caught(self) -> None:
        self.assert_refused(self.store_instead(LineItems=CORRUPTED_LINES[:2]))
        self.assertIn("lines", self.last_detail)

    def test_an_added_line_is_caught(self) -> None:
        self.assert_refused(
            self.store_instead(
                LineItems=[*CORRUPTED_LINES, dict(CORRUPTED_LINES[0])]
            )
        )

    def test_tax_appearing_on_a_line_is_caught(self) -> None:
        """DHL charges no VAT of its own: every line is sent TaxAmount 0."""
        taxed = [dict(line) for line in EXPECTED_LINES]
        taxed[0]["TaxAmount"] = 10.0
        self.assert_refused(self.store_instead(LineItems=taxed))
        self.assertIn("TaxAmount", self.last_detail)

    def test_a_line_stating_no_quantity_cannot_be_verified(self) -> None:
        """An absent value is a difference, never assumed to match."""
        without = [dict(line) for line in EXPECTED_LINES]
        without[0].pop("Quantity")
        self.assert_refused(self.store_instead(LineItems=without))
        self.assertIn("Quantity", self.last_detail)

    def test_reordered_lines_are_caught(self) -> None:
        """Lines are compared in order, not as an unordered bag."""
        self.assert_refused(
            self.store_instead(LineItems=list(reversed(EXPECTED_LINES)))
        )

    def test_a_changed_description_is_caught(self) -> None:
        renamed = [dict(line) for line in EXPECTED_LINES]
        renamed[0]["Description"] = "Something else entirely"
        self.assert_refused(self.store_instead(LineItems=renamed))
        self.assertIn("described", self.last_detail)

    def test_an_honestly_stored_update_still_completes(self) -> None:
        """The verification must not refuse a bill Xero stored correctly."""
        result = self.executor(
            {"documents": [source_for(self.mail, "invoice", "10")]}
        )
        self.assertTrue(result.values["completed"])
        self.assertIn("verified_stored_update", result.values["steps"])
        self.assertEqual(self.xero.authorised, ["bill-1"])


class RestartContinuityTests(unittest.TestCase):
    """The import resumes across a real process boundary.

    A restart is modelled by discarding every in-process object and building a
    fresh analyzer, adapter and executor over the same Xero organisation. The
    first process must leave nothing in memory that the second one needs.
    """

    def setUp(self) -> None:
        self.organisation: dict = {}

    def process(self, mail=None):
        """A fresh runtime, as after a restart."""
        mail = mail or FakeMail()
        xero = FakeXeroBills(self.organisation)
        return mail, xero, executor_for(mail, xero)

    def test_the_invoice_stage_resumes_in_a_new_process(self) -> None:
        mail, _first_xero, first = self.process()
        first(
            {
                "documents": [
                    source_for(mail, "worksheet", "11"),
                    source_for(mail, "sad", "12"),
                ]
            }
        )
        del first, _first_xero

        mail, second_xero, second = self.process()
        result = second({"documents": [source_for(mail, "invoice", "10")]})
        self.assertTrue(result.values["completed"])
        self.assertEqual(result.values["bill"]["Status"], "AUTHORISED")
        self.assertEqual(second_xero.state["created"], 1)

    def test_a_restarted_customs_stage_resumes_one_draft(self) -> None:
        mail, _xero, first = self.process()
        first(
            {
                "documents": [
                    source_for(mail, "worksheet", "11"),
                    source_for(mail, "sad", "12"),
                ]
            }
        )
        del first, _xero

        mail, second_xero, second = self.process()
        result = second(
            {
                "documents": [
                    source_for(mail, "worksheet", "11"),
                    source_for(mail, "sad", "12"),
                ]
            }
        )
        self.assertTrue(result.values["completed"])
        self.assertIn("resumed_verified_draft", result.values["steps"])
        self.assertEqual(second_xero.state["created"], 1)

    def test_a_restart_after_a_failed_attachment_still_recovers(self) -> None:
        """Restart and partial failure together, the realistic case."""
        mail, xero, first = self.process()
        first(
            {
                "documents": [
                    source_for(mail, "worksheet", "11"),
                    source_for(mail, "sad", "12"),
                ]
            }
        )
        xero.attach_bill_document = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("upload failed")
        )
        with self.assertRaises(RuntimeError):
            first({"documents": [source_for(mail, "invoice", "10")]})
        del first, xero

        mail, second_xero, second = self.process()
        result = second({"documents": [source_for(mail, "invoice", "10")]})
        self.assertTrue(result.values["completed"])
        self.assertEqual(second_xero.state["created"], 1)
        self.assertEqual(second_xero.authorised, ["bill-1"])


if __name__ == "__main__":
    unittest.main()
