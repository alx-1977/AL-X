"""Provider-neutral boundary for deterministic DHL document reconciliation."""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence


# Every code a DHL document can fail with. process_dhl_import declares these
# as its possible failures, so this is the one list both sides read. A code
# missing from it reached the broker undeclared and was rewritten to
# result_failure_invalid, which hid what the documents actually said.
DHL_DOCUMENT_FAILURES = (
    "source_mismatch",
    "supporting_document_mismatch",
    "not_a_dhl_invoice",
    "invoice_invalid",
    "invoice_too_large",
    "invoice_format_invalid",
    "invoice_too_many_rows",
    "invoice_number_missing",
    "waybill_missing",
    "invoice_total_missing",
    "invoice_currency_missing",
    "invoice_date_missing",
    "invoice_date_invalid",
    "invoice_date_ambiguous",
    "invoice_amount_invalid",
    "not_customs_worksheet",
    "customs_document_unrecognised",
    "customs_evidence_ambiguous",
    "too_many_customs_documents",
    "worksheet_invalid",
    "worksheet_too_large",
    "worksheet_pdf_invalid",
    "worksheet_too_many_pages",
    "worksheet_too_many_runs",
    "worksheet_content_too_large",
    "worksheet_identity_ambiguous",
    "worksheet_total_missing",
    "sad500_identity_ambiguous",
)


class DhlDocumentError(Exception):
    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not code.strip():
            raise ValueError("code must not be blank")
        # Not ValueError: process_dhl_import reads that as unusable
        # arguments, which would hide an undeclared code as the caller's fault.
        if code not in DHL_DOCUMENT_FAILURES:
            raise LookupError(f"undeclared DHL document failure: {code}")
        self.code = code
        super().__init__(code)


class DhlImportAnalyzer(Protocol):
    """Deterministic reading of DHL documents. It commits nothing."""

    def classify(self, document: bytes) -> str: ...

    def customs_evidence(
        self, customs_documents: Sequence[bytes]
    ) -> Mapping[str, Any]: ...

    def invoice_fields(self, invoice_document: bytes) -> Mapping[str, Any]: ...

    def invoice_evidence(self, structured_document: bytes) -> Mapping[str, Any]: ...
