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

# The instruction asks what the document appears to be, so the answer is a
# free description rather than a fixed token. Matching two exact strings
# rejected a real invoice that described itself as "invoice", so the decision
# is made on what the document says it is, not on one expected spelling.
_NOT_A_BILL = (
    "credit note",
    "credit_note",
    "creditnote",
    "quote",
    "quotation",
    "purchase order",
    "purchase_order",
    "statement",
    "remittance",
    "delivery note",
    "delivery_note",
    "receipt",
    "proforma",
    "pro forma",
)


def is_supplier_bill(document_type: str) -> bool:
    """Decide whether an extracted document is a bill AL/X may post.

    A statement, quote or credit note is a different accounting document and
    must not become a bill. Anything that calls itself an invoice or bill, in
    whatever wording, is one.
    """
    value = document_type.strip().casefold().replace("-", " ")
    if not value:
        return False
    if any(marker in value for marker in _NOT_A_BILL):
        return False
    return "invoice" in value or "bill" in value

INSTRUCTION = """Read the supplier invoice text and return its fields exactly as
printed. Report only what the document states.

An invoice carries several numbers. The invoice number is the supplier's own
reference for this document, not an order number, account number, customer
reference or line-item code. When the document contains more than one plausible
invoice-number candidate and the supplier's actual invoice number cannot be
distinguished reliably from the document alone, return an empty invoice_number.
Do not guess.

Amounts are decimal strings without currency symbols or thousands separators.
Dates are ISO 8601, yyyy-mm-dd. Use an empty string for a field the document
does not state; never infer, calculate or invent a value. If the text is not a
supplier invoice, set document_type to what it appears to be and leave the
invoice fields empty.

Some invoices deduct amounts after the charges: a discount, a promotional or
startup discount, or an account credit applied. List each one in adjustments,
in the order printed, with its printed label as description and its amount as a
negative decimal string. subtotal is then the amount before those deductions.
A payment already received is not an adjustment. amount_due is the amount the
document says is due or charged, when it states one; otherwise leave it empty."""

_FLAT_SCHEMA = json_schema(
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
# Deductions are a list, so they extend the flat schema rather than flattening
# into numbered fields. Each is read as printed; checked_invoice decides nothing
# beyond the arithmetic.
ANSWER_SCHEMA = {
    **_FLAT_SCHEMA,
    "properties": {
        **_FLAT_SCHEMA["properties"],
        "amount_due": {"type": "string"},
        "adjustments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "amount": {"type": "string"},
                },
                "required": ["description", "amount"],
                "additionalProperties": False,
            },
        },
    },
    "required": [*_FLAT_SCHEMA["required"], "amount_due", "adjustments"],
}


def invoice_question(document_text: str, context_line: str = "") -> SpecialistQuestion:
    """Build the bounded question for one document.

    `context_line` is untrusted mail metadata. It may appear as document
    material on the generic specialist path; it is never an identity hint
    and must not be treated as a second source of the invoice number.
    """
    material = document_text
    if context_line.strip():
        material = f"Context (email subject / filename): {context_line}\n\n{material}"
    return SpecialistQuestion(
        EXTRACT_INVOICE, INSTRUCTION, material, ANSWER_SCHEMA
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
    """Check identity, currency presence and arithmetic before relying on fields.

    A model reading a document can misread a number that still looks plausible.
    Nothing here repairs a value: an inconsistency is reported so AL/X can
    judge it, because guessing which figure was wrong is not mechanical.
    """
    problems: list[str] = []
    total = _money(values, "total")
    subtotal = _money(values, "subtotal")
    tax = _money(values, "tax_amount")

    if not _text(values, "currency"):
        problems.append("currency missing")
    if not _text(values, "invoice_number"):
        problems.append("invoice number missing")
    if not _text(values, "supplier_name"):
        problems.append("supplier name missing")
    if total is None:
        problems.append("total missing or unreadable")
    elif total <= 0:
        problems.append(f"total is not a positive amount: {total}")
    amount_due = _money(values, "amount_due")
    # A printed figure that cannot be read is never taken as absent: an
    # unreadable tax would otherwise post a taxed invoice as tax-free.
    for name, parsed in (("tax_amount", tax), ("amount_due", amount_due)):
        if parsed is None and _text(values, name):
            problems.append(f"{name.replace('_', ' ')} unreadable")
    adjustments: list[dict[str, str]] = []
    raw_adjustments = values.get("adjustments") or ()
    if not isinstance(raw_adjustments, (list, tuple)):
        problems.append("adjustments unreadable")
        raw_adjustments = ()
    for item in raw_adjustments:
        label = _text(item, "description") if isinstance(item, Mapping) else ""
        amount = _money(item, "amount") if isinstance(item, Mapping) else None
        if not label or amount is None:
            problems.append("an adjustment has no readable label or amount")
        elif amount >= 0:
            problems.append(f"adjustment {label} {amount} is not a deduction")
        else:
            adjustments.append({"description": label, "amount": format(amount, "f")})
    bill_total = total
    if adjustments:
        # The bill carries the charges and each deduction as its own line, so
        # its total must equal a figure the document prints: the total when
        # the deductions are all above it, the amount due when a credit is
        # applied after it. Matching neither is reported, never resolved.
        if subtotal is None:
            problems.append("adjustments without a subtotal")
        else:
            bill_total = subtotal + (tax or Decimal("0")) + sum(
                Decimal(item["amount"]) for item in adjustments
            )
            printed = [value for value in (total, amount_due) if value is not None]
            if not any(abs(bill_total - value) <= Decimal("0.01") for value in printed):
                problems.append(
                    f"subtotal {subtotal}, tax {tax or 0} and adjustments come to "
                    f"{bill_total}, which is neither the total {total} nor the amount due {amount_due}"
                )
            elif bill_total <= 0:
                problems.append(f"bill total is not a positive amount: {bill_total}")
    elif subtotal is not None and tax is not None and total is not None:
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
        "amount_due": "" if amount_due is None else format(amount_due, "f"),
        "adjustments": tuple(adjustments),
        "bill_total": "" if bill_total is None else format(bill_total, "f"),
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
