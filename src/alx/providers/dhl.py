"""Deterministic DHL MyBill and SARS worksheet reconciliation from V1 evidence."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from pypdf import PdfReader
from pypdf.errors import LimitReachedError

from alx.contracts import DhlDocumentError
from alx.providers.pdf_limits import enforce_pdf_decode_limits


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


def _assessed_on(declaration: str) -> str:
    """The assessment date SARS encodes in the declaration number.

    A customs declaration is `AAAYYYYMMDD` plus a sequence, so the date the
    entry was assessed is stated by the identifier itself rather than inferred.
    Both retained V1 declarations follow it. An identifier that does not carry
    a real date yields nothing: an empty date is refused upstream, which is
    better than posting a bill under a date no document asserts.
    """
    digits = declaration[3:11]
    if len(digits) != 8 or not digits.isdigit():
        return ""
    try:
        return datetime.strptime(digits, "%Y%m%d").date().isoformat()
    except ValueError:
        return ""


def _runs_by_page(payload: bytes) -> list[list[_Run]]:
    payload = _bounded(payload, "worksheet")
    pages: list[list[_Run]] = []
    if not payload.startswith(b"%PDF-"):
        raise DhlDocumentError("worksheet_pdf_invalid")
    invalid_pdf = False
    exceeded = ""
    total_runs = [0]
    try:
        enforce_pdf_decode_limits()
        reader = PdfReader(io.BytesIO(payload), strict=True)
        if len(reader.pages) > _WORKSHEET_PAGES:
            raise _BoundExceeded("worksheet_too_many_pages")
        content_bytes = 0
        for page in reader.pages:
            contents = page.get_contents()
            if contents is None:
                continue
            # get_data() decodes the stream, so a small compressed page could
            # expand before the comparison. The stored bytes are measured
            # first, and only a page that is small on the wire is decoded.
            stored = contents.get_object()
            raw = getattr(stored, "_data", b"") or b""
            content_bytes += len(raw)
            if content_bytes > _WORKSHEET_CONTENT_BYTES:
                raise _BoundExceeded("worksheet_content_too_large")
            content_bytes += len(contents.get_data()) - len(raw)
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
    except LimitReachedError:
        exceeded = "worksheet_content_too_large"
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


_MYBILL_MARKERS = ("Invoice Number", "Line Type", "Shipment Number")


def _mybill_rows(payload: bytes) -> list[dict[str, str]]:
    """The shipment rows of a DHL MyBill CSV, or [] if this is not one.

    MyBill sends this machine-readable export in the same email as the PDF. It
    states the charge codes, per-line amounts, discounts, weight charge,
    declaration number and tax, so classification and reconciliation read it
    rather than recovering figures from a rendered document.
    """
    payload = _bounded(payload, "invoice")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        return []
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return []
    if len(set(header)) != len(header) or not all(
        marker in header for marker in _MYBILL_MARKERS
    ):
        return []
    rows: list[dict[str, str]] = []
    for index, values in enumerate(reader):
        if index >= _INVOICE_ROWS:
            raise DhlDocumentError("invoice_too_many_rows")
        if len(values) != len(header):
            raise DhlDocumentError("invoice_format_invalid")
        row = dict(zip(header, values))
        if (row.get("Line Type") or "").strip().upper() == "S":
            rows.append(row)
    return rows


def _charge_lines(row: Mapping[str, str]) -> tuple[tuple[str, str, Decimal], ...]:
    """Each stated extra charge as (code, name, net amount).

    `XCn Total` is net of that line's discount; `XCn Charge` is gross. V1
    recorded a fuel surcharge billed at 1,374.75 gross against a -601.09
    discount: reading the gross figure overstates the bill by the discount.
    """
    found: list[tuple[str, str, Decimal]] = []
    for index in range(1, 12):
        code = str(row.get(f"XC{index} Code") or "").strip()
        if not code or code == "0":
            continue
        raw_net = row.get(f"XC{index} Total")
        net = _csv_money(raw_net)
        gross = _csv_money(row.get(f"XC{index} Charge"))
        # An explicit zero is DHL's net result after discount; falling back to
        # the gross value would recreate a charge the invoice removed.
        amount = net if str(raw_net or "").strip() else gross
        if not amount:
            continue
        name = str(row.get(f"XC{index} Name") or code).strip()
        found.append((code, name, amount))
    return tuple(found)


def _csv_money(raw: Any) -> Decimal:
    text = str(raw or "").strip().replace(",", "")
    if not text:
        return Decimal("0")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise DhlDocumentError("invoice_amount_invalid")
    if not value.is_finite():
        raise DhlDocumentError("invoice_amount_invalid")
    return value


def classify_dhl_document(payload: bytes) -> str:
    """Name what a DHL-related document is, from its own content.

    The stage a DHL import is at is decided by the documents themselves, never
    by wording: customs evidence begins the first stage, an invoice completes
    it. A document that is neither is refused rather than guessed at.
    """
    rows = _mybill_rows(payload)
    if rows:
        return _classify_mybill(rows)
    try:
        pages = _runs_by_page(payload)
    except DhlDocumentError:
        return "unreadable"
    upper = " ".join(run.text for page in pages for run in page).upper()
    if "CUSTOMS WORKSHEET" in upper:
        return "customs_worksheet"
    if "SAD 500" in upper or "CUSTOMS DECLARATION" in upper:
        return "sad_500"
    if "DHL" in upper and any(label in upper for label in _TOTAL_LABELS):
        return "dhl_invoice"
    return "unrecognised"


def _classify_mybill(rows: Sequence[Mapping[str, str]]) -> str:
    """Which of the three DHL invoice shapes a MyBill export states.

    D-022. The discriminators are ordered and come from the document alone: a
    weight charge means DHL billed its own carriage; otherwise a declaration
    number means SARS assessed an entry, whose worksheet and SAD 500 carry the
    duty and VAT; otherwise the charges are duties and fees paid on an export,
    with no second document behind them.
    """
    if any(_csv_money(row.get("Weight Charge")) for row in rows):
        return "dhl_freight_invoice"
    if any(str(row.get("Declaration/Entry number") or "").strip() for row in rows):
        return "dhl_customs_invoice"
    charges = tuple(line for row in rows for line in _charge_lines(row))
    duty_tax_names = {
        "IMPORT EXPORT DUTIES",
        "REGULATORY CHARGES",
        "DUTY TAX PAID",
    }
    if charges and all(name.upper() in duty_tax_names for _code, name, _amount in charges):
        return "dhl_duty_tax_invoice"
    return "unrecognised"


def _csv_date(raw: Any) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    for pattern in ("%Y%m%d", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            continue
    raise DhlDocumentError("invoice_date_invalid")


def _one_csv_value(
    rows: Sequence[Mapping[str, str]], field: str, code: str
) -> str:
    values = {
        str(row.get(field) or "").strip()
        for row in rows
        if str(row.get(field) or "").strip()
    }
    if len(values) != 1:
        raise DhlDocumentError(code)
    return next(iter(values))


def parse_mybill_invoice(payload: bytes) -> dict[str, Any]:
    """Read and reconcile one structured DHL MyBill invoice.

    This is evidence only: it performs no accounting write. The production
    capability decides what to do with the classified shape.
    """
    rows = _mybill_rows(payload)
    if not rows:
        raise DhlDocumentError("not_a_dhl_invoice")
    kind = _classify_mybill(rows)
    invoice_number = _one_csv_value(rows, "Invoice Number", "invoice_number_missing")
    waybill = _one_csv_value(rows, "Shipment Number", "waybill_missing")
    currency = _one_csv_value(rows, "Currency", "invoice_currency_missing")
    invoice_date = _csv_date(
        _one_csv_value(rows, "Invoice Date", "invoice_date_missing")
    )
    due_values = {
        str(row.get("Due Date") or "").strip()
        for row in rows
        if str(row.get("Due Date") or "").strip()
    }
    if len(due_values) > 1:
        raise DhlDocumentError("invoice_date_ambiguous")
    due_date = _csv_date(next(iter(due_values))) if due_values else ""
    total = _csv_money(
        _one_csv_value(
            rows, "Total amount (incl. VAT)", "invoice_total_missing"
        )
    )
    tax = sum((_csv_money(row.get("Total Tax")) for row in rows), Decimal("0"))
    stated_tax_values = [
        _csv_money(row.get(field))
        for row in rows
        for field in (
            "Total Tax",
            "Weight Tax (VAT)",
            "Total Extra Charges Tax",
            *(f"XC{index} Tax" for index in range(1, 12)),
        )
    ]
    charges = tuple(line for row in rows for line in _charge_lines(row))
    components = sum((amount for _code, _name, amount in charges), Decimal("0"))
    problems: list[str] = []
    if total <= 0:
        problems.append("invoice total is not positive")
    if not _agree(components, total):
        problems.append(f"components {components} do not equal invoice total {total}")
    return {
        "kind": kind,
        "invoice_number": invoice_number,
        "waybill": waybill,
        "currency": currency,
        "invoice_date": invoice_date,
        "due_date": due_date,
        "total": format(total, "f"),
        "tax": format(tax, "f"),
        "tax_present": any(value != 0 for value in stated_tax_values),
        "verified": not problems,
        "problems": tuple(problems),
        "lines": tuple(
            {
                "code": code,
                "description": name,
                "amount": format(amount, "f"),
            }
            for code, name, amount in charges
        ),
    }


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


_INVOICE_NUMBER = re.compile(r"\b([A-Z]{2,}\d{6,})\b")
_TEN_DIGITS = re.compile(r"\b(\d{10})\b")
_WAYBILL_CONTEXT = re.compile(
    r"(?:HAWB|waybill|SHIPMENT)[^0-9]{0,25}(\d{10})", re.IGNORECASE
)
_TOTAL_LABELS = ("NET AMOUNT PAYABLE", "GRAND TOTAL")


def _labelled_amount(joined: str, label: str) -> Decimal | None:
    match = re.search(
        re.escape(label) + r"[^0-9]{0,15}([\d,]+\.\d{2})", joined, re.IGNORECASE
    )
    return _decimal_token(match.group(1)) if match else None


def _labelled_date(joined: str, label: str) -> str:
    """A date stated beside its label, normalised to ISO.

    DHL South Africa states dates as DD/MM/YYYY; ISO is accepted too. Only
    these two unambiguous forms are read. A value that is not a real calendar
    date yields nothing rather than a guess.
    """
    match = re.search(
        re.escape(label) + r"[^0-9]{0,15}(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})",
        joined,
        re.IGNORECASE,
    )
    if not match:
        return ""
    value = match.group(1)
    for pattern in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            continue
    return ""


def parse_invoice_pdf(payload: bytes) -> dict[str, Any]:
    """Read the fields the invoice stage needs from a DHL MyBill invoice PDF.

    The invoice that arrives by email is a PDF, not the structured MyBill CSV.
    Only the waybill, number, total and dates are read: the clearance charge is
    never parsed here, because it is the invoice total less the duty and VAT
    already verified from the customs documents. That subtraction reconciles
    exactly and avoids guessing charge-code layouts that vary by invoice.
    """
    pages = _runs_by_page(payload)
    joined = " ".join(run.text for page in pages for run in page)
    upper = joined.upper()
    if "DHL" not in upper:
        raise DhlDocumentError("not_a_dhl_invoice")

    number_match = _INVOICE_NUMBER.search(joined)
    invoice_number = number_match.group(1) if number_match else ""
    if not invoice_number:
        raise DhlDocumentError("invoice_number_missing")

    # Several ten-digit values appear on an invoice, so the labelled one wins
    # and the most repeated is the fallback: a waybill repeats per charge line
    # while a VAT or registration number appears once.
    context = _WAYBILL_CONTEXT.search(joined)
    if context:
        waybill = context.group(1)
    else:
        found = _TEN_DIGITS.findall(joined)
        waybill = max(set(found), key=found.count) if found else ""
    if not waybill:
        raise DhlDocumentError("waybill_missing")

    total = None
    for label in _TOTAL_LABELS:
        total = _labelled_amount(joined, label)
        if total is not None:
            break
    if total is None or total <= 0:
        raise DhlDocumentError("invoice_total_missing")

    due_date = _labelled_date(joined, "DUE DATE")
    # Only a distinctly labelled invoice date is accepted. A bare "DATE" on a
    # DHL invoice matches the due-date label, which would post the due date as
    # the invoice date. Empty is better than guessed.
    invoice_date = _labelled_date(joined, "INVOICE DATE")
    if invoice_date == due_date:
        invoice_date = ""

    return {
        "invoice_number": invoice_number,
        "waybill": waybill,
        "total": format(total, "f"),
        "invoice_date": invoice_date,
        "due_date": due_date,
    }


class DhlImportAnalyzerAdapter:
    def classify(self, document: bytes) -> str:
        return classify_dhl_document(document)

    def invoice_fields(self, invoice_document: bytes) -> Mapping[str, Any]:
        return parse_invoice_pdf(invoice_document)

    def invoice_evidence(self, structured_document: bytes) -> Mapping[str, Any]:
        return parse_mybill_invoice(structured_document)

    def customs_evidence(
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
            # The two figures the provisional bill is drafted from, kept apart
            # because import VAT is claimable and duty is not.
            "duty": format(worksheet.duty, "f"),
            "vat": format(worksheet.vat, "f"),
            "assessed_on": _assessed_on(worksheet.declaration),
            "problems": tuple(errors),
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
