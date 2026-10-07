"""D-038: read what one commercial document attached to a mail says.

One language-blind primitive. AL/X names an exact attachment and its digest;
the document is read against a fixed schema (issuer, recipient, number, date,
cited documents, totals, lines) and the values come back as read. Nothing here
decides what the document is for, which quote or order it matches, or whether
anything should happen next.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from alx.contracts import (
    CapabilityDefinition,
    CapabilityResult,
    CapabilityResultState,
    MailAccessError,
    MailAccount,
    MailReference,
    SideEffect,
    SpecialistError,
    StructuredData,
    StructuredSchema,
    ValueKind,
)
from alx.contracts.provenance import RetentionPolicy
from alx.specialists.commercial_document import DOCUMENT_FIELDS


READ_MAIL_DOCUMENT = "read_mail_document"

_STRING = StructuredSchema(ValueKind.STRING)
_MAX_LINES = 200
_MAX_TEXT = 2_000

DEFINITION = CapabilityDefinition(
    READ_MAIL_DOCUMENT,
    "Read one commercial document attached to a mail (a purchase order, quotation, invoice, delivery note or statement; PDF or image) and return what it states: document_type, document_number, issued_by, issued_to, date, currency, the other document numbers it references, subtotal, tax_amount, total and every line with description, quantity, unit_price and amount, as printed. Empty strings mean the document does not state it. Name the exact attachment and its sha256 from list_mail_attachments. Read the document rather than relying on an attachment's filename.",
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "mailbox_id": _STRING,
            "uid_validity": _STRING,
            "uid": _STRING,
            "attachment_id": _STRING,
            "expected_sha256": _STRING,
        },
        ("mailbox_id", "uid_validity", "uid", "attachment_id", "expected_sha256"),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {"filename": _STRING, "document": StructuredSchema(ValueKind.OBJECT)},
        ("filename", "document"),
        extra_properties=False,
    ),
    SideEffect.NONE,
    (
        "arguments_unusable",
        "attachment_unavailable",
        "source_mismatch",
        "connection_failed",
        "authentication_failed",
        "mailbox_unavailable",
        "message_unavailable",
        "document_has_no_text",
        "document_too_large",
        "unsupported_media_type",
        "extraction_timeout",
        "provider_failed",
        "answer_not_structured",
    ),
)

DEFINITIONS = (DEFINITION,)


def _text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format(Decimal(str(value)), "f")
    return str(value)[:_MAX_TEXT] if isinstance(value, str) else ""


def _document(values: Mapping[str, Any]) -> dict[str, Any]:
    """The schema's fields only, bounded, as text."""
    document: dict[str, Any] = {}
    for name in DOCUMENT_FIELDS:
        value = values.get(name)
        if name == "references":
            document[name] = tuple(
                _text(item) for item in (value or ())[:_MAX_LINES] if _text(item)
            ) if isinstance(value, (list, tuple)) else ()
        elif name == "lines":
            document[name] = tuple(
                {
                    key: _text(line.get(key))
                    for key in ("description", "quantity", "unit_price", "amount")
                }
                for line in (value or ())[:_MAX_LINES]
                if isinstance(line, Mapping)
            ) if isinstance(value, (list, tuple)) else ()
        else:
            document[name] = _text(value)
    return document


def build_document_executors(
    mail: MailAccount,
    reader: Callable[[bytes, str, str], Mapping[str, Any]],
    call_id_source: Callable[[], str],
    clock: Callable[[], datetime] | None = None,
) -> Mapping[str, Callable[[StructuredData], CapabilityResult]]:
    now = clock or (lambda: datetime.now(UTC))

    def failed(code: str) -> CapabilityResult:
        return CapabilityResult(
            call_id_source(), READ_MAIL_DOCUMENT, CapabilityResultState.FAILED,
            failure={"code": code},
        )

    def read_document(arguments: StructuredData) -> CapabilityResult:
        try:
            values = {
                name: arguments.get(name)
                for name in ("mailbox_id", "uid_validity", "uid", "attachment_id",
                             "expected_sha256")
            }
            if any(not isinstance(item, str) or not item.strip() for item in values.values()):
                raise ValueError("arguments")
            reference = MailReference(
                values["mailbox_id"], values["uid_validity"], values["uid"]
            )
            attachment, payload = mail.read_attachment(reference, values["attachment_id"])
            if attachment.sha256 != values["expected_sha256"].strip().lower():
                return failed("source_mismatch")
            read = reader(payload, attachment.media_type, attachment.filename)
            if not isinstance(read, Mapping):
                return failed("answer_not_structured")
        except ValueError:
            return failed("arguments_unusable")
        except MailAccessError as error:
            return failed(error.code)
        except SpecialistError as error:
            return failed(error.code)
        return CapabilityResult(
            call_id_source(),
            READ_MAIL_DOCUMENT,
            CapabilityResultState.SUCCEEDED,
            {"filename": attachment.filename, "document": _document(read)},
            # What a mailed document says is mail content, under D-013.
            provenance=RetentionPolicy().direct_mail(now(), (reference,)),
        )

    return {READ_MAIL_DOCUMENT: read_document}
