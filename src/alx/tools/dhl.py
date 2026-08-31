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
ANALYZE_DHL_CUSTOMS_DOCUMENTS = "analyze_dhl_customs_documents"

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
_DOCUMENT_EVIDENCE = StructuredSchema(
    ValueKind.OBJECT,
    {"kind": _STRING, "source": _SOURCE},
    ("kind", "source"),
    extra_properties=False,
)

_FAILURES = (
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
    "worksheet_identity_ambiguous",
    "worksheet_total_missing",
    "not_sad500",
    "sad500_identity_ambiguous",
    "customs_document_unrecognised",
    "customs_evidence_ambiguous",
)

ANALYZE_DEFINITION = CapabilityDefinition(
    ANALYZE_DHL_CUSTOMS_DOCUMENTS,
    "Analyse one exact DHL Customs Worksheet and its matching SAD 500 before the MyBill invoice exists; return a provisional evidence proposal without changing mail or Xero.",
    StructuredSchema(
        ValueKind.OBJECT,
        {"customs_documents": StructuredSchema(ValueKind.ARRAY, items=_SOURCE)},
        ("customs_documents",),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "verified": _BOOLEAN,
            "provisional_invoice_number": _STRING,
            "currency": _STRING,
            "total": _STRING,
            "waybill": _STRING,
            "declaration": _STRING,
            "reference": _STRING,
            "lines": StructuredSchema(ValueKind.ARRAY, items=_LINE),
            "documents": StructuredSchema(ValueKind.ARRAY, items=_DOCUMENT_EVIDENCE),
            "errors": StructuredSchema(ValueKind.ARRAY, items=_STRING),
            "warnings": StructuredSchema(ValueKind.ARRAY, items=_STRING),
        },
        (
            "verified",
            "provisional_invoice_number",
            "currency",
            "total",
            "waybill",
            "declaration",
            "reference",
            "lines",
            "documents",
            "errors",
            "warnings",
        ),
        extra_properties=False,
    ),
    SideEffect.NONE,
    _FAILURES,
)

DEFINITION = CapabilityDefinition(
    RECONCILE_DHL_IMPORT_DOCUMENTS,
    "Deterministically reconcile one DHL MyBill CSV attachment with the exact Customs Worksheet and SAD 500 attachments supplied for its shipments; return a bill proposal without changing mail or Xero.",
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
            "documents": StructuredSchema(ValueKind.ARRAY, items=_DOCUMENT_EVIDENCE),
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
            "documents",
            "errors",
            "warnings",
        ),
        extra_properties=False,
    ),
    SideEffect.NONE,
    _FAILURES,
)

DEFINITIONS = (ANALYZE_DEFINITION, DEFINITION)


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


def _normalised_source(values: Mapping[str, Any]) -> dict[str, str]:
    return {
        name: _required(values, name)
        for name in (
            "mailbox_id",
            "uid_validity",
            "uid",
            "attachment_id",
            "expected_sha256",
        )
    }


def _with_document_evidence(
    values: Mapping[str, Any], sources: list[dict[str, str]]
) -> dict[str, Any]:
    kinds = values.get("document_kinds")
    if not isinstance(kinds, (tuple, list)) or len(kinds) != len(sources):
        raise DhlDocumentError("customs_evidence_ambiguous")
    output = dict(values)
    output.pop("document_kinds", None)
    output["documents"] = tuple(
        {"kind": str(kind), "source": source}
        for kind, source in zip(kinds, sources, strict=True)
    )
    return output


def build_dhl_executors(
    mail: MailAccount,
    analyzer: DhlImportAnalyzer,
    call_id_source: Callable[[], str],
    clock: Callable[[], datetime] | None = None,
) -> Mapping[str, Callable[[StructuredData], CapabilityResult]]:
    now = clock or (lambda: datetime.now(UTC))

    def failure(capability_id: str, code: str) -> CapabilityResult:
        return CapabilityResult(
            call_id_source(),
            capability_id,
            CapabilityResultState.FAILED,
            failure={"code": code},
        )

    def analyze_customs(arguments: StructuredData) -> CapabilityResult:
        references: list[MailReference] = []
        sources: list[dict[str, str]] = []
        try:
            customs_sources = arguments.get("customs_documents")
            if not isinstance(customs_sources, (tuple, list)) or not customs_sources:
                raise ValueError("customs_documents")
            payloads: list[bytes] = []
            for source in customs_sources:
                if not isinstance(source, Mapping):
                    raise ValueError("customs_documents")
                reference, payload = _read_source(mail, source)
                references.append(reference)
                sources.append(_normalised_source(source))
                payloads.append(payload)
            values = _with_document_evidence(
                analyzer.analyze_customs(payloads), sources
            )
        except ValueError:
            return failure(ANALYZE_DHL_CUSTOMS_DOCUMENTS, "arguments_unusable")
        except MailAccessError as error:
            return failure(ANALYZE_DHL_CUSTOMS_DOCUMENTS, error.code)
        except DhlDocumentError as error:
            return failure(ANALYZE_DHL_CUSTOMS_DOCUMENTS, error.code)
        return CapabilityResult(
            call_id_source(),
            ANALYZE_DHL_CUSTOMS_DOCUMENTS,
            CapabilityResultState.SUCCEEDED,
            values,
            provenance=RetentionPolicy().direct_mail(now(), tuple(references)),
        )

    def reconcile(arguments: StructuredData) -> CapabilityResult:
        references: list[MailReference] = []
        sources: list[dict[str, str]] = []
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
                sources.append(_normalised_source(source))
                customs_payloads.append(payload)
            invoice_number = arguments.get("invoice_number", "")
            if not isinstance(invoice_number, str):
                raise ValueError("invoice_number")
            values = _with_document_evidence(
                analyzer.reconcile(
                    invoice_payload, customs_payloads, invoice_number.strip()
                ),
                sources,
            )
        except ValueError:
            return failure(RECONCILE_DHL_IMPORT_DOCUMENTS, "arguments_unusable")
        except MailAccessError as error:
            return failure(RECONCILE_DHL_IMPORT_DOCUMENTS, error.code)
        except DhlDocumentError as error:
            return failure(RECONCILE_DHL_IMPORT_DOCUMENTS, error.code)
        return CapabilityResult(
            call_id_source(),
            RECONCILE_DHL_IMPORT_DOCUMENTS,
            CapabilityResultState.SUCCEEDED,
            values,
            provenance=RetentionPolicy().direct_mail(now(), tuple(references)),
        )

    return {
        ANALYZE_DHL_CUSTOMS_DOCUMENTS: analyze_customs,
        RECONCILE_DHL_IMPORT_DOCUMENTS: reconcile,
    }
