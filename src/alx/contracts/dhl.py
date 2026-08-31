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
    def analyze_customs(
        self, customs_documents: Sequence[bytes]
    ) -> Mapping[str, Any]: ...

    def reconcile(
        self,
        invoice_document: bytes,
        customs_documents: Sequence[bytes],
        invoice_number: str = "",
    ) -> Mapping[str, Any]: ...
