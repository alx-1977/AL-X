"""Language-blind DHL document reconciliation primitive."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from alx.contracts import (
    CapabilityDefinition,
    CapabilityResult,
    CapabilityResultState,
    DhlDocumentError,
    DhlImportAnalyzer,
    MailAccessError,
    MailAccount,
    MailReference,
    SideEffect,
    StructuredData,
    StructuredSchema,
    ValueKind,
)
from alx.contracts.provenance import RetentionPolicy


RECONCILE_DHL_IMPORT_DOCUMENTS = "reconcile_dhl_import_documents"

_STRING = StructuredSchema(ValueKind.STRING)
_BOOLEAN = StructuredSchema(ValueKind.BOOLEAN)
_SOURCE = StructuredSchema(
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
)
_LINE = StructuredSchema(
    ValueKind.OBJECT,
    {
        "category": _STRING,
        "description": _STRING,
        "amount": _STRING,
        "claimable": _BOOLEAN,
    },
    ("category", "description", "amount", "claimable"),
    extra_properties=False,
)

DEFINITION = CapabilityDefinition(
    RECONCILE_DHL_IMPORT_DOCUMENTS,
    "Deterministically reconcile one DHL MyBill CSV attachment with the exact SARS customs worksheet attachments supplied for its shipments; return a bill proposal without changing mail or Xero.",
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "invoice_document": _SOURCE,
            "customs_documents": StructuredSchema(ValueKind.ARRAY, items=_SOURCE),
            "invoice_number": _STRING,
        },
        ("invoice_document", "customs_documents"),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "reconciled": _BOOLEAN,
            "invoice_number": _STRING,
            "invoice_date": _STRING,
            "due_date": _STRING,
            "currency": _STRING,
            "total": _STRING,
            "waybills": StructuredSchema(ValueKind.ARRAY, items=_STRING),
            "declarations": StructuredSchema(ValueKind.ARRAY, items=_STRING),
            "reference": _STRING,
            "lines": StructuredSchema(ValueKind.ARRAY, items=_LINE),
            "errors": StructuredSchema(ValueKind.ARRAY, items=_STRING),
            "warnings": StructuredSchema(ValueKind.ARRAY, items=_STRING),
        },
        (
            "reconciled",
            "invoice_number",
            "invoice_date",
            "due_date",
            "currency",
            "total",
            "waybills",
            "declarations",
            "reference",
            "lines",
            "errors",
            "warnings",
        ),
        extra_properties=False,
    ),
    SideEffect.NONE,
    (
        "arguments_unusable",
        "attachment_unavailable",
        "source_mismatch",
        "invoice_encoding_invalid",
        "invoice_format_invalid",
        "invoice_header_missing",
        "invoice_unavailable",
        "invoice_selection_required",
        "invoice_number_unavailable",
        "worksheet_pdf_invalid",
        "not_customs_worksheet",
        "worksheet_identity_missing",
        "worksheet_total_missing",
    ),
)

DEFINITIONS = (DEFINITION,)


def _required(values: Mapping[str, Any], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name)
    return value.strip()


def _read_source(
    mail: MailAccount, values: Mapping[str, Any]
) -> tuple[MailReference, bytes]:
    reference = MailReference(
        _required(values, "mailbox_id"),
        _required(values, "uid_validity"),
        _required(values, "uid"),
    )
    attachment, payload = mail.read_attachment(
        reference, _required(values, "attachment_id")
    )
    if attachment.sha256 != _required(values, "expected_sha256"):
        raise DhlDocumentError("source_mismatch")
    return reference, payload


def build_dhl_executors(
    mail: MailAccount,
    analyzer: DhlImportAnalyzer,
    call_id_source: Callable[[], str],
    clock: Callable[[], datetime] | None = None,
) -> Mapping[str, Callable[[StructuredData], CapabilityResult]]:
    now = clock or (lambda: datetime.now(UTC))

    def reconcile(arguments: StructuredData) -> CapabilityResult:
        references: list[MailReference] = []
        try:
            invoice_source = arguments.get("invoice_document")
            customs_sources = arguments.get("customs_documents")
            if not isinstance(invoice_source, Mapping) or not isinstance(
                customs_sources, (tuple, list)
            ) or not customs_sources:
                raise ValueError("documents")
            invoice_reference, invoice_payload = _read_source(mail, invoice_source)
            references.append(invoice_reference)
            customs_payloads: list[bytes] = []
            for source in customs_sources:
                if not isinstance(source, Mapping):
                    raise ValueError("customs_documents")
                reference, payload = _read_source(mail, source)
                references.append(reference)
                customs_payloads.append(payload)
            invoice_number = arguments.get("invoice_number", "")
            if not isinstance(invoice_number, str):
                raise ValueError("invoice_number")
            values = analyzer.reconcile(
                invoice_payload, customs_payloads, invoice_number.strip()
            )
        except ValueError:
            return CapabilityResult(
                call_id_source(),
                RECONCILE_DHL_IMPORT_DOCUMENTS,
                CapabilityResultState.FAILED,
                failure={"code": "arguments_unusable"},
            )
        except MailAccessError as error:
            return CapabilityResult(
                call_id_source(),
                RECONCILE_DHL_IMPORT_DOCUMENTS,
                CapabilityResultState.FAILED,
                failure={"code": error.code},
            )
        except DhlDocumentError as error:
            return CapabilityResult(
                call_id_source(),
                RECONCILE_DHL_IMPORT_DOCUMENTS,
                CapabilityResultState.FAILED,
                failure={"code": error.code},
            )
        return CapabilityResult(
            call_id_source(),
            RECONCILE_DHL_IMPORT_DOCUMENTS,
            CapabilityResultState.SUCCEEDED,
            values,
            provenance=RetentionPolicy().direct_mail(now(), tuple(references)),
        )

    return {RECONCILE_DHL_IMPORT_DOCUMENTS: reconcile}
