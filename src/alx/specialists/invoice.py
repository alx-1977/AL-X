"""Read a supplier invoice into structured fields, and nothing more.

The instruction below is the specialist's entire world. It states what to read
and what to return, and deliberately grants no authority: this call cannot
decide whether a bill should exist, which account it belongs to, or what
happens next. Those are AL/X's, or deterministic code's.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from alx.contracts import SpecialistError, SpecialistQuestion
from alx.specialists.runner import json_schema


EXTRACT_INVOICE = "extract_supplier_invoice"

INSTRUCTION = """Read the supplier invoice text and return its fields exactly as
printed. Report only what the document states.

An invoice carries several numbers. The invoice number is the supplier's own
reference for this document, not an order number, account number, customer
reference or line-item code. Where the context line names a number that also
appears in the document, prefer that one.

Amounts are decimal strings without currency symbols or thousands separators.
Dates are ISO 8601, yyyy-mm-dd. Use an empty string for a field the document
does not state; never infer, calculate or invent a value. If the text is not a
supplier invoice, set document_type to what it appears to be and leave the
invoice fields empty."""

_ANSWER_SCHEMA = json_schema(
    {
        "document_type": "string",
        "supplier_name": "string",
        "invoice_number": "string",
        "invoice_date": "string",
        "due_date": "string",
        "currency": "string",
        "subtotal": "string",
        "tax_amount": "string",
        "total": "string",
        "description": "string",
    },
    (
        "document_type",
        "supplier_name",
        "invoice_number",
        "invoice_date",
        "due_date",
        "currency",
        "subtotal",
        "tax_amount",
        "total",
        "description",
    ),
)


def invoice_question(document_text: str, context_line: str = "") -> SpecialistQuestion:
    """Build the bounded question for one document.

    `context_line` is the email subject and filename. A number appearing both
    there and in the document is almost certainly the invoice number, and an
    unstable choice would create duplicate bills.
    """
    material = document_text
    if context_line.strip():
        material = f"Context (email subject / filename): {context_line}\n\n{material}"
    return SpecialistQuestion(
        EXTRACT_INVOICE, INSTRUCTION, material, _ANSWER_SCHEMA
    )


def _text(values: Mapping[str, Any], name: str) -> str:
    value = values.get(name)
    return value.strip() if isinstance(value, str) else ""


def _money(values: Mapping[str, Any], name: str) -> Decimal | None:
    raw = _text(values, name).replace(",", "").replace(" ", "")
    if not raw:
        return None
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        return None
    return amount if amount.is_finite() else None


def checked_invoice(values: Mapping[str, Any]) -> dict[str, Any]:
    """Verify the extracted figures arithmetically before anyone relies on them.

    A model reading a document can misread a number that still looks plausible.
    Nothing here repairs a value: an inconsistency is reported so AL/X can
    judge it, because guessing which figure was wrong is not mechanical.
    """
    problems: list[str] = []
    total = _money(values, "total")
    subtotal = _money(values, "subtotal")
    tax = _money(values, "tax_amount")

    if not _text(values, "invoice_number"):
        problems.append("invoice number missing")
    if not _text(values, "supplier_name"):
        problems.append("supplier name missing")
    if total is None:
        problems.append("total missing or unreadable")
    elif total <= 0:
        problems.append(f"total is not a positive amount: {total}")
    if subtotal is not None and tax is not None and total is not None:
        if abs((subtotal + tax) - total) > Decimal("0.01"):
            problems.append(
                f"subtotal {subtotal} plus tax {tax} does not equal total {total}"
            )
    return {
        "document_type": _text(values, "document_type"),
        "supplier_name": _text(values, "supplier_name"),
        "invoice_number": _text(values, "invoice_number"),
        "invoice_date": _text(values, "invoice_date"),
        "due_date": _text(values, "due_date"),
        "currency": _text(values, "currency"),
        "subtotal": "" if subtotal is None else format(subtotal, "f"),
        "tax_amount": "" if tax is None else format(tax, "f"),
        "total": "" if total is None else format(total, "f"),
        "description": _text(values, "description"),
        "verified": not problems,
        "problems": tuple(problems),
    }


def extract_invoice(
    specialist: Any, document_text: str, context_line: str = ""
) -> dict[str, Any]:
    """Extract and check one invoice. Returns data; decides nothing."""
    if not document_text.strip():
        raise SpecialistError("document_has_no_text")
    return checked_invoice(specialist.answer(invoice_question(document_text, context_line)))
