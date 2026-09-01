"""Provider-neutral boundary for deterministic DHL document reconciliation."""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence


class DhlDocumentError(Exception):
    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not code.strip():
            raise ValueError("code must not be blank")
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
