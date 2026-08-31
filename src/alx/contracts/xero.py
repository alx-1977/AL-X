"""Provider-neutral contracts for the approved Xero accounting boundary."""

from __future__ import annotations

from typing import Any, Mapping, Protocol


class XeroAccessError(Exception):
    """A sanitised Xero failure carrying no request, token, or document."""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not code.strip():
            raise ValueError("code must not be blank")
        self.code = code
        super().__init__(code)


class XeroAccountingAccount(Protocol):
    def search_contacts(self, search_term: str) -> tuple[Mapping[str, Any], ...]: ...

    def list_accounts(self) -> tuple[Mapping[str, Any], ...]: ...

    def list_tax_rates(self) -> tuple[Mapping[str, Any], ...]: ...

    def find_bill(
        self, invoice_number: str, contact_id: str = ""
    ) -> Mapping[str, Any] | None: ...

    def read_bill(self, invoice_id: str) -> Mapping[str, Any] | None: ...

    def create_draft_bill(self, bill: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def attach_bill_document(
        self,
        invoice_id: str,
        filename: str,
        media_type: str,
        content: bytes,
    ) -> Mapping[str, Any]: ...

    def authorise_bill(self, invoice_id: str) -> Mapping[str, Any]: ...
