"""Language-blind Xero primitives for supplier accounts-payable bills."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from alx.contracts import (
    CapabilityDefinition,
    CapabilityResult,
    CapabilityResultState,
    MailAccessError,
    MailAccount,
    MailReference,
    SideEffect,
    StructuredData,
    StructuredSchema,
    ValueKind,
    XeroAccessError,
    XeroAccountingAccount,
)


SEARCH_XERO_CONTACTS = "search_xero_contacts"
LIST_XERO_ACCOUNTS = "list_xero_accounts"
LIST_XERO_TAX_RATES = "list_xero_tax_rates"
FIND_XERO_BILL = "find_xero_bill"
READ_XERO_BILL = "read_xero_bill"
CREATE_XERO_DRAFT_BILL = "create_xero_draft_bill"
ATTACH_MAIL_DOCUMENT_TO_XERO_BILL = "attach_mail_document_to_xero_bill"
AUTHORISE_XERO_BILL = "authorise_xero_bill"

_FAILURES = (
    "arguments_unusable",
    "connection_failed",
    "not_connected",
    "token_unreadable",
    "token_key_invalid",
    "oauth_rejected",
    "permission_denied",
    "rate_limited",
    "request_rejected",
    "response_invalid",
    "contact_not_found",
    "bill_not_found",
    "duplicate_found",
    "bill_not_draft",
    "supporting_document_missing",
    "source_mismatch",
    "attachment_unavailable",
)

_STRING = StructuredSchema(ValueKind.STRING)
_BOOLEAN = StructuredSchema(ValueKind.BOOLEAN)
_INTEGER = StructuredSchema(ValueKind.INTEGER)
_ANY_OBJECT = StructuredSchema(ValueKind.OBJECT)
_ARRAY_OBJECT = StructuredSchema(ValueKind.ARRAY, items=_ANY_OBJECT)


def _object(
    properties: Mapping[str, StructuredSchema],
    required: Sequence[str],
) -> StructuredSchema:
    return StructuredSchema(
        ValueKind.OBJECT,
        properties,
        tuple(required),
        extra_properties=False,
    )


_BILL_RESULT = _object(
    {
        "found": _BOOLEAN,
        "invoice_id": _STRING,
        "invoice_number": _STRING,
        "contact_id": _STRING,
        "contact_name": _STRING,
        "status": _STRING,
        "currency": _STRING,
        "total": _STRING,
        "amount_due": _STRING,
        "reference": _STRING,
        "has_attachments": _BOOLEAN,
    },
    (
        "found",
        "invoice_id",
        "invoice_number",
        "contact_id",
        "contact_name",
        "status",
        "currency",
        "total",
        "amount_due",
        "reference",
        "has_attachments",
    ),
)

_LINE_ITEM = _object(
    {
        "description": _STRING,
        "quantity": _STRING,
        "unit_amount": _STRING,
        "account_code": _STRING,
        "tax_type": _STRING,
        "tax_amount": _STRING,
    },
    ("description", "quantity", "unit_amount", "account_code", "tax_type", "tax_amount"),
)

SEARCH_CONTACTS_DEFINITION = CapabilityDefinition(
    SEARCH_XERO_CONTACTS,
    "Search existing contacts in the configured Xero organisation without creating or changing one.",
    _object({"search_term": _STRING}, ("search_term",)),
    _object({"contacts": _ARRAY_OBJECT}, ("contacts",)),
    SideEffect.NONE,
    _FAILURES,
)

LIST_ACCOUNTS_DEFINITION = CapabilityDefinition(
    LIST_XERO_ACCOUNTS,
    "List the configured Xero chart-of-accounts entries without changing them.",
    _object({}, ()),
    _object({"accounts": _ARRAY_OBJECT}, ("accounts",)),
    SideEffect.NONE,
    _FAILURES,
)

LIST_TAX_RATES_DEFINITION = CapabilityDefinition(
    LIST_XERO_TAX_RATES,
    "List the configured Xero tax rates without changing them.",
    _object({}, ()),
    _object({"tax_rates": _ARRAY_OBJECT}, ("tax_rates",)),
    SideEffect.NONE,
    _FAILURES,
)

FIND_BILL_DEFINITION = CapabilityDefinition(
    FIND_XERO_BILL,
    "Find an accounts-payable bill by its exact supplier invoice number without changing it.",
    _object(
        {"invoice_number": _STRING, "contact_id": _STRING},
        ("invoice_number",),
    ),
    _BILL_RESULT,
    SideEffect.NONE,
    _FAILURES,
)

READ_BILL_DEFINITION = CapabilityDefinition(
    READ_XERO_BILL,
    "Read one exact Xero accounts-payable bill by Xero identifier without changing it.",
    _object({"invoice_id": _STRING}, ("invoice_id",)),
    _BILL_RESULT,
    SideEffect.NONE,
    _FAILURES,
)

CREATE_DRAFT_DEFINITION = CapabilityDefinition(
    CREATE_XERO_DRAFT_BILL,
    "Create one DRAFT accounts-payable bill from explicit, balanced structured values; refuse an existing invoice number.",
    _object(
        {
            "contact_id": _STRING,
            "invoice_number": _STRING,
            "date": _STRING,
            "due_date": _STRING,
            "currency": _STRING,
            "reference": _STRING,
            "line_amount_types": _STRING,
            "expected_total": _STRING,
            "line_items": StructuredSchema(ValueKind.ARRAY, items=_LINE_ITEM),
        },
        (
            "contact_id",
            "invoice_number",
            "date",
            "due_date",
            "currency",
            "reference",
            "line_amount_types",
            "expected_total",
            "line_items",
        ),
    ),
    _BILL_RESULT,
    SideEffect.EFFECTFUL,
    _FAILURES,
)

ATTACH_DOCUMENT_DEFINITION = CapabilityDefinition(
    ATTACH_MAIL_DOCUMENT_TO_XERO_BILL,
    "Attach one exact, hash-matched MIME attachment from an identified mail item to one Xero bill.",
    _object(
        {
            "invoice_id": _STRING,
            "mailbox_id": _STRING,
            "uid_validity": _STRING,
            "uid": _STRING,
            "attachment_id": _STRING,
            "expected_sha256": _STRING,
        },
        (
            "invoice_id",
            "mailbox_id",
            "uid_validity",
            "uid",
            "attachment_id",
            "expected_sha256",
        ),
    ),
    _object(
        {
            "invoice_id": _STRING,
            "filename": _STRING,
            "sha256": _STRING,
            "attached": _BOOLEAN,
        },
        ("invoice_id", "filename", "sha256", "attached"),
    ),
    SideEffect.EFFECTFUL,
    _FAILURES,
)

AUTHORISE_BILL_DEFINITION = CapabilityDefinition(
    AUTHORISE_XERO_BILL,
    "Authorise one exact DRAFT Xero bill only after its invoice number and total match the supplied expected values.",
    _object(
        {
            "invoice_id": _STRING,
            "invoice_number": _STRING,
            "expected_total": _STRING,
        },
        ("invoice_id", "invoice_number", "expected_total"),
    ),
    _BILL_RESULT,
    SideEffect.EFFECTFUL,
    _FAILURES,
)

DEFINITIONS = (
    SEARCH_CONTACTS_DEFINITION,
    LIST_ACCOUNTS_DEFINITION,
    LIST_TAX_RATES_DEFINITION,
    FIND_BILL_DEFINITION,
    READ_BILL_DEFINITION,
    CREATE_DRAFT_DEFINITION,
    ATTACH_DOCUMENT_DEFINITION,
    AUTHORISE_BILL_DEFINITION,
)


def _required(arguments: StructuredData, name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name)
    return value.strip()


def _decimal(value: Any, name: str) -> Decimal:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name)
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(name) from error
    if not parsed.is_finite():
        raise ValueError(name)
    return parsed


def _money(value: Any) -> str:
    try:
        return format(Decimal(str(value or "0")).quantize(Decimal("0.01")), "f")
    except InvalidOperation:
        return "0.00"


def _bill_values(bill: Mapping[str, Any] | None) -> dict[str, Any]:
    if bill is None:
        return {
            "found": False,
            "invoice_id": "",
            "invoice_number": "",
            "contact_id": "",
            "contact_name": "",
            "status": "",
            "currency": "",
            "total": "0.00",
            "amount_due": "0.00",
            "reference": "",
            "has_attachments": False,
        }
    contact = bill.get("Contact") if isinstance(bill.get("Contact"), Mapping) else {}
    return {
        "found": True,
        "invoice_id": str(bill.get("InvoiceID") or ""),
        "invoice_number": str(bill.get("InvoiceNumber") or ""),
        "contact_id": str(contact.get("ContactID") or ""),
        "contact_name": str(contact.get("Name") or ""),
        "status": str(bill.get("Status") or ""),
        "currency": str(bill.get("CurrencyCode") or ""),
        "total": _money(bill.get("Total")),
        "amount_due": _money(bill.get("AmountDue")),
        "reference": str(bill.get("Reference") or ""),
        "has_attachments": bool(bill.get("HasAttachments")),
    }


def _line_items(arguments: StructuredData) -> tuple[list[dict[str, Any]], Decimal]:
    values = arguments.get("line_items")
    if not isinstance(values, (tuple, list)) or not values:
        raise ValueError("line_items")
    line_amount_types = _required(arguments, "line_amount_types")
    if line_amount_types not in ("Exclusive", "Inclusive", "NoTax"):
        raise ValueError("line_amount_types")
    output: list[dict[str, Any]] = []
    total = Decimal("0")
    for item in values:
        if not isinstance(item, Mapping):
            raise ValueError("line_items")
        description = _required(item, "description")
        account_code = _required(item, "account_code")
        tax_type = _required(item, "tax_type")
        quantity = _decimal(item.get("quantity"), "quantity")
        unit_amount = _decimal(item.get("unit_amount"), "unit_amount")
        tax_amount = _decimal(item.get("tax_amount"), "tax_amount")
        if quantity <= 0 or unit_amount < 0 or tax_amount < 0:
            raise ValueError("line_items")
        base = quantity * unit_amount
        total += base + tax_amount if line_amount_types == "Exclusive" else base
        output.append(
            {
                "Description": description,
                "Quantity": float(quantity),
                "UnitAmount": float(unit_amount),
                "AccountCode": account_code,
                "TaxType": tax_type,
                "TaxAmount": float(tax_amount),
            }
        )
    return output, total.quantize(Decimal("0.01"))


def build_xero_executors(
    account: XeroAccountingAccount,
    mail: MailAccount,
    call_id_source: Callable[[], str],
) -> Mapping[str, Callable[[StructuredData], CapabilityResult]]:
    def failed(capability_id: str, code: str) -> CapabilityResult:
        return CapabilityResult(
            call_id_source(),
            capability_id,
            CapabilityResultState.FAILED,
            failure={"code": code},
        )

    def invoke(
        capability_id: str, function: Callable[[], Mapping[str, Any]]
    ) -> CapabilityResult:
        try:
            values = function()
        except ValueError:
            return failed(capability_id, "arguments_unusable")
        except XeroAccessError as error:
            return failed(capability_id, error.code)
        return CapabilityResult(
            call_id_source(), capability_id, CapabilityResultState.SUCCEEDED, values
        )

    def search_contacts(arguments: StructuredData) -> CapabilityResult:
        def operation() -> Mapping[str, Any]:
            contacts = account.search_contacts(_required(arguments, "search_term"))
            return {
                "contacts": tuple(
                    {
                        "contact_id": str(item.get("ContactID") or ""),
                        "name": str(item.get("Name") or ""),
                        "email": str(item.get("EmailAddress") or ""),
                        "status": str(item.get("ContactStatus") or ""),
                        "is_supplier": bool(item.get("IsSupplier")),
                    }
                    for item in contacts
                )
            }

        return invoke(SEARCH_XERO_CONTACTS, operation)

    def list_accounts(_arguments: StructuredData) -> CapabilityResult:
        return invoke(
            LIST_XERO_ACCOUNTS,
            lambda: {
                "accounts": tuple(
                    {
                        "account_id": str(item.get("AccountID") or ""),
                        "code": str(item.get("Code") or ""),
                        "name": str(item.get("Name") or ""),
                        "type": str(item.get("Type") or ""),
                        "status": str(item.get("Status") or ""),
                        "tax_type": str(item.get("TaxType") or ""),
                    }
                    for item in account.list_accounts()
                )
            },
        )

    def list_tax_rates(_arguments: StructuredData) -> CapabilityResult:
        return invoke(
            LIST_XERO_TAX_RATES,
            lambda: {
                "tax_rates": tuple(
                    {
                        "name": str(item.get("Name") or ""),
                        "tax_type": str(item.get("TaxType") or ""),
                        "status": str(item.get("Status") or ""),
                        "effective_rate": str(item.get("EffectiveRate") or ""),
                    }
                    for item in account.list_tax_rates()
                )
            },
        )

    def find_bill(arguments: StructuredData) -> CapabilityResult:
        contact_id = arguments.get("contact_id", "")
        if not isinstance(contact_id, str):
            return failed(FIND_XERO_BILL, "arguments_unusable")
        return invoke(
            FIND_XERO_BILL,
            lambda: _bill_values(
                account.find_bill(
                    _required(arguments, "invoice_number"), contact_id.strip()
                )
            ),
        )

    def read_bill(arguments: StructuredData) -> CapabilityResult:
        return invoke(
            READ_XERO_BILL,
            lambda: _bill_values(account.read_bill(_required(arguments, "invoice_id"))),
        )

    def create_draft(arguments: StructuredData) -> CapabilityResult:
        def operation() -> Mapping[str, Any]:
            contact_id = _required(arguments, "contact_id")
            invoice_number = _required(arguments, "invoice_number")
            issue_date = _required(arguments, "date")
            due_date = _required(arguments, "due_date")
            date.fromisoformat(issue_date)
            date.fromisoformat(due_date)
            expected_total = _decimal(arguments.get("expected_total"), "expected_total")
            lines, calculated_total = _line_items(arguments)
            if calculated_total != expected_total.quantize(Decimal("0.01")):
                raise ValueError("expected_total")
            if account.find_bill(invoice_number, contact_id) is not None:
                raise XeroAccessError("duplicate_found")
            bill = {
                "Type": "ACCPAY",
                "Contact": {"ContactID": contact_id},
                "InvoiceNumber": invoice_number,
                "Date": issue_date,
                "DueDate": due_date,
                "CurrencyCode": _required(arguments, "currency"),
                "Reference": _required(arguments, "reference"),
                "LineAmountTypes": _required(arguments, "line_amount_types"),
                "LineItems": lines,
                "Status": "DRAFT",
            }
            created = account.create_draft_bill(bill)
            values = _bill_values(created)
            if values["invoice_number"] != invoice_number or Decimal(values["total"]) != expected_total:
                raise XeroAccessError("source_mismatch")
            return values

        return invoke(CREATE_XERO_DRAFT_BILL, operation)

    def attach_document(arguments: StructuredData) -> CapabilityResult:
        try:
            invoice_id = _required(arguments, "invoice_id")
            reference = MailReference(
                _required(arguments, "mailbox_id"),
                _required(arguments, "uid_validity"),
                _required(arguments, "uid"),
            )
            attachment, content = mail.read_attachment(
                reference, _required(arguments, "attachment_id")
            )
            if attachment.sha256 != _required(arguments, "expected_sha256"):
                return failed(ATTACH_MAIL_DOCUMENT_TO_XERO_BILL, "source_mismatch")
            account.attach_bill_document(
                invoice_id,
                attachment.filename,
                attachment.media_type,
                content,
            )
        except ValueError:
            return failed(ATTACH_MAIL_DOCUMENT_TO_XERO_BILL, "arguments_unusable")
        except MailAccessError as error:
            return failed(ATTACH_MAIL_DOCUMENT_TO_XERO_BILL, error.code)
        except XeroAccessError as error:
            return failed(ATTACH_MAIL_DOCUMENT_TO_XERO_BILL, error.code)
        return CapabilityResult(
            call_id_source(),
            ATTACH_MAIL_DOCUMENT_TO_XERO_BILL,
            CapabilityResultState.SUCCEEDED,
            {
                "invoice_id": invoice_id,
                "filename": attachment.filename,
                "sha256": attachment.sha256,
                "attached": True,
            },
        )

    def authorise(arguments: StructuredData) -> CapabilityResult:
        def operation() -> Mapping[str, Any]:
            invoice_id = _required(arguments, "invoice_id")
            invoice_number = _required(arguments, "invoice_number")
            expected_total = _decimal(arguments.get("expected_total"), "expected_total")
            current = _bill_values(account.read_bill(invoice_id))
            if not current["found"]:
                raise XeroAccessError("bill_not_found")
            if current["status"] != "DRAFT":
                raise XeroAccessError("bill_not_draft")
            if not current["has_attachments"]:
                raise XeroAccessError("supporting_document_missing")
            if (
                current["invoice_number"] != invoice_number
                or Decimal(current["total"]) != expected_total
            ):
                raise XeroAccessError("source_mismatch")
            return _bill_values(account.authorise_bill(invoice_id))

        return invoke(AUTHORISE_XERO_BILL, operation)

    return {
        SEARCH_XERO_CONTACTS: search_contacts,
        LIST_XERO_ACCOUNTS: list_accounts,
        LIST_XERO_TAX_RATES: list_tax_rates,
        FIND_XERO_BILL: find_bill,
        READ_XERO_BILL: read_bill,
        CREATE_XERO_DRAFT_BILL: create_draft,
        ATTACH_MAIL_DOCUMENT_TO_XERO_BILL: attach_document,
        AUTHORISE_XERO_BILL: authorise,
    }
