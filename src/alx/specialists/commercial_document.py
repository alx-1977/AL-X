"""What a commercial document states, read by LlamaCloud for AL/X.

A purchase order, quotation, invoice, delivery note or statement is read into
one fixed shape: who issued it to whom, its number and date, the documents it
cites, its totals and every line. Nothing here decides what the document is
for or whether it matches anything; that is AL/X's judgement, made from these
values. On 2026-10-06 she could not read BlueNova's PO at all and matched it
to a quote by the attachment's filename.
"""

from __future__ import annotations

DOCUMENT_INSTRUCTION = """Read this commercial document and return what it
states, exactly as printed. Report only what the document says.

document_type is what the document is: purchase_order, quotation, invoice,
credit_note, delivery_note, statement, receipt or other. document_number is the
issuer's own number for this document. references lists every other document
number it cites, such as a quote number on a purchase order or an order number
on an invoice, each exactly as printed. issued_by and issued_to are the
organisations named as issuer and recipient.

lines lists every line item in order, with its description, quantity, unit
price and line amount as printed. Amounts and quantities are decimal strings
without currency symbols or thousands separators. Dates are ISO 8601,
yyyy-mm-dd. Use an empty string for anything the document does not state;
never infer, calculate or invent a value."""

_LINE_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "quantity": {"type": "string"},
        "unit_price": {"type": "string"},
        "amount": {"type": "string"},
    },
    "required": ["description", "quantity", "unit_price", "amount"],
    "additionalProperties": False,
}

DOCUMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "document_type": {"type": "string"},
        "document_number": {"type": "string"},
        "issued_by": {"type": "string"},
        "issued_to": {"type": "string"},
        "date": {"type": "string"},
        "currency": {"type": "string"},
        "references": {"type": "array", "items": {"type": "string"}},
        "subtotal": {"type": "string"},
        "tax_amount": {"type": "string"},
        "total": {"type": "string"},
        "lines": {"type": "array", "items": _LINE_SCHEMA},
    },
    "required": [
        "document_type", "document_number", "issued_by", "issued_to", "date",
        "currency", "references", "subtotal", "tax_amount", "total", "lines",
    ],
    "additionalProperties": False,
}

DOCUMENT_FIELDS = tuple(DOCUMENT_SCHEMA["properties"])
