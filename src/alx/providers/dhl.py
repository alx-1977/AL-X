"""Deterministic DHL MyBill and SARS worksheet reconciliation from V1 evidence."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from pypdf import PdfReader

from alx.contracts import DhlDocumentError


TOLERANCE = Decimal("0.01")

# A mail attachment is untrusted input. The archive path already bounds what it
# will expand; a document handed over directly was parsed without any limit, so
# one large or pathological CSV or PDF could exhaust memory or CPU and take the
# local runtime down. These mirror the archive member limits.
_DOCUMENT_BYTES = 25 * 1024 * 1024
_INVOICE_ROWS = 20_000
_WORKSHEET_PAGES = 100
_WORKSHEET_RUNS = 200_000
# pypdf decodes a page's whole content stream before the visitor sees a single
# run, so no per-run check can prevent the work: aborting on the first run of a
# hostile page still cost twelve seconds. The stream is measured first, which
# takes milliseconds. A real worksheet is tens of kilobytes.
_WORKSHEET_CONTENT_BYTES = 4 * 1024 * 1024
_CUSTOMS_DOCUMENTS = 100


class _BoundExceeded(Exception):
    """Carry a bound failure past the broad PDF error handler."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _bounded(payload: bytes, name: str) -> bytes:
    """Refuse a document too large to parse before parsing any of it."""
    if not isinstance(payload, (bytes, bytearray)):
        raise DhlDocumentError(f"{name}_invalid")
    if len(payload) > _DOCUMENT_BYTES:
        raise DhlDocumentError(f"{name}_too_large")
    return bytes(payload)
_MONEY_PATTERN = r"[\d,]+\.\d{2}"
_SERVICE_CODES = frozenset({"WC", "WE"})
_KNOWN_CODES = frozenset({"XX", "XB", *_SERVICE_CODES})


@dataclass(frozen=True, slots=True)
class Charge:
    code: str
    name: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class Shipment:
    waybill: str
    declaration: str
    charges: tuple[Charge, ...]


@dataclass(frozen=True, slots=True)
class Invoice:
    invoice_number: str
    invoice_date: date
    due_date: date
    currency: str
    total: Decimal
    shipments: tuple[Shipment, ...]

    @property
    def charges(self) -> tuple[Charge, ...]:
        return tuple(charge for shipment in self.shipments for charge in shipment.charges)


@dataclass(frozen=True, slots=True)
class Worksheet:
    declaration: str
    waybill: str
    duty: Decimal
    vat: Decimal
    total: Decimal


@dataclass(frozen=True, slots=True)
class _Run:
    y: float
    x: float
    text: str


def _money(raw: Any, name: str) -> Decimal:
    text = str(raw or "").strip().replace(",", "")
    if not text:
        return Decimal("0")
    invalid = False
    value = Decimal("0")
    try:
        value = Decimal(text)
    except InvalidOperation:
        invalid = True
    if invalid:
        raise DhlDocumentError(f"{name}_invalid")
    if not value.is_finite():
        raise DhlDocumentError(f"{name}_invalid")
    return value


def _date(raw: Any, name: str) -> date:
    value = str(raw or "").strip()
    invalid = False
    parsed = date.min
    try:
        parsed = datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        invalid = True
    if invalid:
        raise DhlDocumentError(f"{name}_invalid")
    return parsed


def _charges(row: Mapping[str, Any]) -> tuple[Charge, ...]:
    found: list[Charge] = []
    for index in range(1, 12):
        code = str(row.get(f"XC{index} Code") or "").strip()
        if not code or code == "0":
            continue
        amount = _money(row.get(f"XC{index} Charge"), "charge")
        if amount == 0:
            continue
        found.append(
            Charge(
                code,
                str(row.get(f"XC{index} Name") or "").strip(),
                amount,
            )
        )
    return tuple(found)


def _parse_invoices(payload: bytes) -> tuple[Invoice, ...]:
    payload = _bounded(payload, "invoice")
    invalid_encoding = False
    text = ""
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        invalid_encoding = True
    if invalid_encoding:
        raise DhlDocumentError("invoice_encoding_invalid")
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for row in reader:
        if len(rows) >= _INVOICE_ROWS:
            raise DhlDocumentError("invoice_too_many_rows")
        rows.append(row)
    if not rows or "Invoice Number" not in (rows[0] or {}):
        raise DhlDocumentError("invoice_format_invalid")
    headers: dict[str, Mapping[str, Any]] = {}
    shipments: dict[str, list[Shipment]] = {}
    for row in rows:
        number = str(row.get("Invoice Number") or "").strip()
        line_type = str(row.get("Line Type") or "").strip().upper()
        if not number:
            continue
        if line_type == "I":
            headers[number] = row
        elif line_type == "S":
            shipments.setdefault(number, []).append(
                Shipment(
                    str(row.get("Shipment Number") or "").strip(),
                    str(row.get("Declaration/Entry number") or "").strip(),
                    _charges(row),
                )
            )
    invoices: list[Invoice] = []
    for number, legs in shipments.items():
        header = headers.get(number)
        if header is None:
            raise DhlDocumentError("invoice_header_missing")
        invoices.append(
            Invoice(
                number,
                _date(header.get("Invoice Date"), "invoice_date"),
                _date(header.get("Due Date"), "due_date"),
                str(header.get("Currency") or "").strip(),
                _money(header.get("Total amount (incl. VAT)"), "invoice_total"),
                tuple(legs),
            )
        )
    if not invoices:
        raise DhlDocumentError("invoice_unavailable")
    return tuple(invoices)


def _runs_by_page(payload: bytes) -> list[list[_Run]]:
    payload = _bounded(payload, "worksheet")
    pages: list[list[_Run]] = []
    if not payload.startswith(b"%PDF-"):
        raise DhlDocumentError("worksheet_pdf_invalid")
    invalid_pdf = False
    exceeded = ""
    total_runs = [0]
    try:
        reader = PdfReader(io.BytesIO(payload), strict=True)
        if len(reader.pages) > _WORKSHEET_PAGES:
            raise _BoundExceeded("worksheet_too_many_pages")
        content_bytes = 0
        for page in reader.pages:
            contents = page.get_contents()
            content_bytes += len(contents.get_data()) if contents is not None else 0
            if content_bytes > _WORKSHEET_CONTENT_BYTES:
                raise _BoundExceeded("worksheet_content_too_large")
        for page_index, page in enumerate(reader.pages):
            current: list[_Run] = []

            def visitor(text, _cm, tm, _font, _size, *, _page=page_index) -> None:
                stripped = text.strip()
                if not stripped:
                    return
                # Counting only after the page finished let one page with
                # millions of runs be retained in full before the limit was
                # noticed, so the ceiling is enforced as each run arrives.
                if total_runs[0] >= _WORKSHEET_RUNS:
                    raise _BoundExceeded("worksheet_too_many_runs")
                total_runs[0] += 1
                current.append(
                    _Run(
                        round(tm[5], 1) - _page * 100_000,
                        round(tm[4], 1),
                        stripped,
                    )
                )

            page.extract_text(visitor_text=visitor)
            pages.append(current)
    except _BoundExceeded as error:
        exceeded = error.code
    except Exception:
        invalid_pdf = True
    if invalid_pdf:
        raise DhlDocumentError("worksheet_pdf_invalid")
    if exceeded:
        raise DhlDocumentError(exceeded)
    return pages


def _decimal_token(value: str) -> Decimal:
    return Decimal(value.replace(",", ""))


def _value_on_baseline(
    runs: Sequence[_Run], label: str, *, x_min: float = 0, tolerance: float = 3
) -> Decimal | None:
    for anchor in (run for run in runs if label in run.text):
        candidates: list[tuple[float, str]] = []
        for run in runs:
            if abs(run.y - anchor.y) > tolerance or run.x < x_min:
                continue
            for match in re.finditer(_MONEY_PATTERN, run.text):
                if run is anchor and match.start() < len(label):
                    continue
                candidates.append((run.x, match.group(0)))
        if candidates:
            candidates.sort()
            return _decimal_token(candidates[0][1])
    return None


def _first_value(pages: Sequence[Sequence[_Run]], label: str) -> Decimal | None:
    for page in pages:
        value = _value_on_baseline(page, label, x_min=430)
        if value is not None:
            return value
    return None


def _one_identifier(pattern: str, joined: str, code: str) -> str:
    values = tuple(dict.fromkeys(re.findall(pattern, joined)))
    if len(values) != 1:
        raise DhlDocumentError(code)
    return values[0]


def _worksheet_from_pages(pages: Sequence[Sequence[_Run]]) -> Worksheet:
    runs = [run for page in pages for run in page]
    joined = " ".join(run.text for run in runs)
    declaration = _one_identifier(
        r"\b([A-Z]{3}\d{15,})\b", joined, "worksheet_identity_ambiguous"
    )
    waybill = _one_identifier(
        r"\b(\d{10})\b", joined, "worksheet_identity_ambiguous"
    )
    duty = _first_value(pages, "TOTAL DUTY")
    vat = _first_value(pages, "TOTAL VAT")
    if duty is None or vat is None:
        raise DhlDocumentError("worksheet_total_missing")
    total = _first_value(pages, "37.Totals")
    if total is None:
        for run in runs:
            match = re.search(
                rf"({_MONEY_PATTERN})\s+({_MONEY_PATTERN})\s+{_MONEY_PATTERN}$",
                run.text,
            )
            if match and any(
                abs(other.y - run.y) <= 2 and "TotalTotal" in other.text
                for other in runs
            ):
                total = _decimal_token(match.group(2))
                break
    if total is None:
        raise DhlDocumentError("worksheet_total_missing")
    return Worksheet(declaration, waybill, duty, vat, total)


def _parse_worksheet(payload: bytes) -> Worksheet:
    pages = _runs_by_page(payload)
    runs = [run for page in pages for run in page]
    joined = " ".join(run.text for run in runs)
    if "CUSTOMS WORKSHEET" not in joined.upper():
        raise DhlDocumentError("not_customs_worksheet")
    return _worksheet_from_pages(pages)


def _parse_sad500(payload: bytes) -> str:
    pages = _runs_by_page(payload)
    joined = " ".join(run.text for page in pages for run in page)
    upper = joined.upper()
    if "SAD 500" not in upper and "CUSTOMS DECLARATION" not in upper:
        raise DhlDocumentError("not_sad500")
    return _one_identifier(
        r"\b([A-Z]{3}\d{15,})\b", joined, "sad500_identity_ambiguous"
    )


def _classify_customs_document(payload: bytes) -> tuple[str, Worksheet | str]:
    pages = _runs_by_page(payload)
    joined = " ".join(run.text for page in pages for run in page)
    upper = joined.upper()
    if "CUSTOMS WORKSHEET" in upper:
        return "customs_worksheet", _worksheet_from_pages(pages)
    if "SAD 500" in upper or "CUSTOMS DECLARATION" in upper:
        declaration = _one_identifier(
            r"\b([A-Z]{3}\d{15,})\b", joined, "sad500_identity_ambiguous"
        )
        return "sad_500", declaration
    raise DhlDocumentError("customs_document_unrecognised")


def _agree(left: Decimal, right: Decimal) -> bool:
    return abs(left - right) <= TOLERANCE


class DhlImportAnalyzerAdapter:
    def analyze_customs(
        self, customs_documents: Sequence[bytes]
    ) -> Mapping[str, Any]:
        if len(customs_documents) > _CUSTOMS_DOCUMENTS:
            raise DhlDocumentError("too_many_customs_documents")
        classified = tuple(
            _classify_customs_document(payload) for payload in customs_documents
        )
        worksheets = tuple(
            value for kind, value in classified if kind == "customs_worksheet"
        )
        sad_declarations = tuple(
            value for kind, value in classified if kind == "sad_500"
        )
        if len(worksheets) != 1 or len(sad_declarations) != 1:
            raise DhlDocumentError("customs_evidence_ambiguous")
        worksheet = worksheets[0]
        if not isinstance(worksheet, Worksheet) or not isinstance(
            sad_declarations[0], str
        ):
            raise DhlDocumentError("customs_evidence_ambiguous")
        errors: list[str] = []
        if worksheet.declaration != sad_declarations[0]:
            errors.append(
                f"SAD 500 declaration {sad_declarations[0]} != worksheet "
                f"{worksheet.declaration}"
            )
        if not _agree(worksheet.duty + worksheet.vat, worksheet.total):
            errors.append(
                f"worksheet does not balance: duty {worksheet.duty} + VAT "
                f"{worksheet.vat} != {worksheet.total}"
            )
        lines: list[dict[str, Any]] = []
        if worksheet.vat:
            lines.append(
                {
                    "category": "import_vat",
                    "description": "Import VAT (claimable — per SAD 500)",
                    "amount": format(worksheet.vat, "f"),
                    "claimable": True,
                }
            )
        if worksheet.duty:
            lines.append(
                {
                    "category": "customs_duty",
                    "description": "Customs duty (not claimable)",
                    "amount": format(worksheet.duty, "f"),
                    "claimable": False,
                }
            )
        return {
            "verified": not errors,
            "provisional_invoice_number": f"DHL-WAYBILL-{worksheet.waybill}",
            "currency": "ZAR",
            "total": format(worksheet.total, "f"),
            "waybill": worksheet.waybill,
            "declaration": worksheet.declaration,
            "reference": (
                f"DHL import; waybill {worksheet.waybill}; "
                f"declaration {worksheet.declaration}; invoice pending"
            ),
            "lines": tuple(lines),
            "document_kinds": tuple(kind for kind, _value in classified),
            "errors": tuple(errors),
            "warnings": (
                "Provisional evidence only; MyBill invoice and final total pending.",
            ),
        }

    def reconcile(
        self,
        invoice_document: bytes,
        customs_documents: Sequence[bytes],
        invoice_number: str = "",
    ) -> Mapping[str, Any]:
        if len(customs_documents) > _CUSTOMS_DOCUMENTS:
            raise DhlDocumentError("too_many_customs_documents")
        invoices = _parse_invoices(invoice_document)
        if invoice_number:
            selected = [item for item in invoices if item.invoice_number == invoice_number]
            if len(selected) != 1:
                raise DhlDocumentError("invoice_number_unavailable")
            invoice = selected[0]
        elif len(invoices) == 1:
            invoice = invoices[0]
        else:
            raise DhlDocumentError("invoice_selection_required")
        classified = tuple(
            _classify_customs_document(payload) for payload in customs_documents
        )
        worksheets = tuple(
            value for kind, value in classified
            if kind == "customs_worksheet" and isinstance(value, Worksheet)
        )
        sad_declarations = tuple(
            value for kind, value in classified
            if kind == "sad_500" and isinstance(value, str)
        )
        errors: list[str] = []
        warnings: list[str] = []

        by_waybill = {worksheet.waybill: worksheet for worksheet in worksheets}
        if len(by_waybill) != len(worksheets):
            errors.append("duplicate customs worksheet for a waybill")
        missing = sorted(
            shipment.waybill
            for shipment in invoice.shipments
            if shipment.waybill and shipment.waybill not in by_waybill
        )
        if missing:
            errors.append(f"customs worksheet missing for waybill(s): {', '.join(missing)}")
        invoice_waybills = {shipment.waybill for shipment in invoice.shipments}
        unexpected = sorted(set(by_waybill) - invoice_waybills)
        if unexpected:
            errors.append(f"worksheet waybill not on invoice: {', '.join(unexpected)}")
        required_declarations = {
            worksheet.declaration for worksheet in worksheets
        }
        available_sad = set(sad_declarations)
        missing_sad = sorted(required_declarations - available_sad)
        unexpected_sad = sorted(available_sad - required_declarations)
        if missing_sad:
            errors.append(f"SAD 500 missing for declaration(s): {', '.join(missing_sad)}")
        if unexpected_sad:
            errors.append(
                f"SAD 500 declaration not on worksheet: {', '.join(unexpected_sad)}"
            )
        if len(available_sad) != len(sad_declarations):
            errors.append("duplicate SAD 500 for a declaration")

        for shipment in invoice.shipments:
            worksheet = by_waybill.get(shipment.waybill)
            if worksheet is None:
                continue
            if shipment.declaration and worksheet.declaration != shipment.declaration:
                errors.append(
                    f"declaration mismatch for {shipment.waybill}: "
                    f"invoice {shipment.declaration}, worksheet {worksheet.declaration}"
                )
            if not _agree(worksheet.duty + worksheet.vat, worksheet.total):
                errors.append(
                    f"worksheet does not balance for {shipment.waybill}: "
                    f"duty {worksheet.duty} + VAT {worksheet.vat} != {worksheet.total}"
                )

        unknown = [charge for charge in invoice.charges if charge.code not in _KNOWN_CODES]
        for charge in unknown:
            errors.append(
                f"unrecognised charge code {charge.code!r} ({charge.name}) "
                f"{invoice.currency} {charge.amount}"
            )
        charge_total = sum((charge.amount for charge in invoice.charges), Decimal("0"))
        if not _agree(charge_total, invoice.total):
            errors.append(f"invoice lines {charge_total} != invoice total {invoice.total}")

        invoice_duty = sum(
            (charge.amount for charge in invoice.charges if charge.code == "XX"),
            Decimal("0"),
        )
        invoice_vat = sum(
            (charge.amount for charge in invoice.charges if charge.code == "XB"),
            Decimal("0"),
        )
        worksheet_duty = sum((item.duty for item in worksheets), Decimal("0"))
        worksheet_vat = sum((item.vat for item in worksheets), Decimal("0"))
        if not _agree(invoice_duty, worksheet_duty):
            errors.append(f"duty mismatch: invoice {invoice_duty}, worksheets {worksheet_duty}")
        if not _agree(invoice_vat, worksheet_vat):
            errors.append(f"VAT mismatch: invoice {invoice_vat}, worksheets {worksheet_vat}")

        lines: list[dict[str, Any]] = []
        if invoice_vat:
            lines.append(
                {
                    "category": "import_vat",
                    "description": "Import VAT (claimable — per SAD 500)",
                    "amount": format(invoice_vat, "f"),
                    "claimable": True,
                }
            )
        if invoice_duty:
            lines.append(
                {
                    "category": "customs_duty",
                    "description": "Customs duty (not claimable)",
                    "amount": format(invoice_duty, "f"),
                    "claimable": False,
                }
            )
        service_totals: dict[str, Decimal] = {}
        for charge in invoice.charges:
            if charge.code in _SERVICE_CODES:
                name = charge.name.title()
                service_totals[name] = service_totals.get(name, Decimal("0")) + charge.amount
        for name, amount in service_totals.items():
            lines.append(
                {
                    "category": "clearance_fee",
                    "description": name,
                    "amount": format(amount, "f"),
                    "claimable": False,
                }
            )
        proposed_total = sum((Decimal(line["amount"]) for line in lines), Decimal("0"))
        if not _agree(proposed_total, invoice.total):
            errors.append(f"proposed bill lines {proposed_total} != invoice total {invoice.total}")

        waybills = tuple(shipment.waybill for shipment in invoice.shipments)
        declarations = tuple(shipment.declaration for shipment in invoice.shipments)
        return {
            "reconciled": not errors,
            "invoice_number": invoice.invoice_number,
            "invoice_date": invoice.invoice_date.isoformat(),
            "due_date": invoice.due_date.isoformat(),
            "currency": invoice.currency,
            "total": format(invoice.total, "f"),
            "waybills": waybills,
            "declarations": declarations,
            "reference": (
                f"DHL import; waybill(s) {', '.join(waybills)}; "
                f"declaration(s) {', '.join(declarations)}"
            ),
            "lines": tuple(lines),
            "document_kinds": tuple(kind for kind, _value in classified),
            "errors": tuple(errors),
            "warnings": tuple(warnings),
        }
