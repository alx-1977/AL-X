"""Provider-neutral contracts for the approved Xero accounting boundary."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Mapping, Protocol

# Xero's serialised date form, an external protocol format.
_SERIALISED_DATE = re.compile(r"/Date\((-?\d+)([+-]\d{4})?\)/")


def xero_date(value: str) -> str | None:
    """Normalise a Xero date to plain ISO text, or None when unreadable.

    Xero sends either ISO text or its serialised /Date(ms+offset)/ form. This
    decodes one external wire format; treating an unrecognised value as
    matching let a bill dated 1970 pass as current.
    """
    serialised = str(value or "").strip()
    if not serialised:
        return None
    if (
        len(serialised) >= 10
        and serialised[:4].isdigit()
        and serialised[4:5] == "-"
    ):
        return serialised[:10]
    match = _SERIALISED_DATE.fullmatch(serialised)
    if match:
        try:
            return datetime.fromtimestamp(
                int(match.group(1)) / 1000, UTC
            ).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return None


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

    def bills_for_contact(self, contact_id: str) -> tuple[Mapping[str, Any], ...]: ...

    def read_bill(self, invoice_id: str) -> Mapping[str, Any] | None: ...

    def create_draft_bill(self, bill: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def update_draft_bill(
        self, invoice_id: str, bill: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    def attach_bill_document(
        self,
        invoice_id: str,
        filename: str,
        media_type: str,
        content: bytes,
    ) -> Mapping[str, Any]: ...

    def list_bill_attachments(
        self, invoice_id: str
    ) -> tuple[Mapping[str, Any], ...]: ...

    def read_bill_attachment(
        self, invoice_id: str, attachment_id: str, media_type: str
    ) -> bytes: ...

    def delete_draft_bill(self, invoice_id: str) -> Mapping[str, Any]: ...

    def authorise_bill(self, invoice_id: str) -> Mapping[str, Any]: ...
