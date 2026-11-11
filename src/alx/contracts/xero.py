"""Provider-neutral contracts for the approved Xero accounting boundary."""

from __future__ import annotations

from alx.contracts.diagnostics import service_message

import re
from datetime import UTC, datetime
from enum import Enum
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


# A contact create was sent and may have succeeded, but nothing proves it.
CONTACT_CREATION_UNCONFIRMED = "contact_creation_unconfirmed"


class XeroAccessError(Exception):
    """A Xero failure retaining only its code and bounded, masked service diagnostic."""

    def __init__(self, code: str, detail: str = "") -> None:
        if not isinstance(code, str) or not code.strip():
            raise ValueError("code must not be blank")
        self.service_message = service_message(detail) if detail else ""
        self.code = code
        super().__init__(code)


class XeroAccountingAccount(Protocol):
    def search_contacts(
        self, search_term: str, include_archived: bool = False
    ) -> tuple[Mapping[str, Any], ...]: ...

    def read_contact(self, contact_id: str) -> Mapping[str, Any] | None: ...

    def rename_contact(self, contact_id: str, name: str) -> Mapping[str, Any]: ...

    def create_contact(self, name: str) -> Mapping[str, Any]: ...

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

    def read_quote(self, quote_id: str) -> Mapping[str, Any] | None: ...

    def quotes_for_contact(self, contact_id: str) -> tuple[Mapping[str, Any], ...]: ...

    def set_quote_status(
        self, quote_record: Mapping[str, Any], status: str
    ) -> Mapping[str, Any]: ...

    def create_draft_sales_invoice(
        self, invoice: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    def sales_invoices_for_contact(
        self, contact_id: str
    ) -> tuple[Mapping[str, Any], ...]: ...

    def read_sales_invoice(self, invoice_id: str) -> Mapping[str, Any] | None: ...


XERO = "xero"


class XeroAction(str, Enum):
    """Every accounting job, a closed list like the mail and KiCad actions (D-074)."""

    SEARCH_CONTACTS = "search_contacts"
    CREATE_CONTACT = "create_contact"
    UPDATE_CONTACT = "update_contact"
    LIST_ACCOUNTS = "list_accounts"
    LIST_TAX_RATES = "list_tax_rates"
    FIND_BILL = "find_bill"
    READ_BILL = "read_bill"
    DELETE_DRAFT_BILL = "delete_draft_bill"
    CAPTURE_INVOICE = "capture_invoice"
    CAPTURE_DIGIKEY_BILL = "capture_digikey_bill"
    IMPORT_DHL = "import_dhl"
    FIND_QUOTES = "find_quotes"
    INVOICE_QUOTE = "invoice_quote"
    LOOKUP_DIGIKEY = "lookup_digikey"


# Goals saved before D-074 hold calls under the fourteen former capability ids.
# They are only recognised here, so a resumed goal still knows what it already
# captured or imported; none of them can be called (Law 0).
_RECORDED_BEFORE_D074 = {
    "search_xero_contacts": XeroAction.SEARCH_CONTACTS,
    "create_xero_contact": XeroAction.CREATE_CONTACT,
    "update_xero_contact": XeroAction.UPDATE_CONTACT,
    "list_xero_accounts": XeroAction.LIST_ACCOUNTS,
    "list_xero_tax_rates": XeroAction.LIST_TAX_RATES,
    "find_xero_bill": XeroAction.FIND_BILL,
    "read_xero_bill": XeroAction.READ_BILL,
    "delete_xero_draft_bill": XeroAction.DELETE_DRAFT_BILL,
    "capture_supplier_invoice": XeroAction.CAPTURE_INVOICE,
    "capture_digikey_order_bill": XeroAction.CAPTURE_DIGIKEY_BILL,
    "process_dhl_import": XeroAction.IMPORT_DHL,
    "find_xero_quotes": XeroAction.FIND_QUOTES,
    "invoice_xero_quote": XeroAction.INVOICE_QUOTE,
    "lookup_digikey_order": XeroAction.LOOKUP_DIGIKEY,
}


def recorded_xero_action(capability_id: str, arguments: Mapping[str, Any]) -> XeroAction | None:
    """Which accounting job a recorded call was, in today's form or as saved before D-074."""
    if capability_id == XERO:
        try:
            return XeroAction(arguments.get("action"))
        except ValueError:
            return None
    return _RECORDED_BEFORE_D074.get(capability_id)
