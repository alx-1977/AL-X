"""Language-blind Xero primitives for supplier accounts-payable bills."""

from __future__ import annotations

import hashlib
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
    SpecialistError,
    XeroAccessError,
    XeroAccountingAccount,
)
from alx.specialists import is_supplier_bill, prior_coding, resolve_supplier


SEARCH_XERO_CONTACTS = "search_xero_contacts"
LIST_XERO_ACCOUNTS = "list_xero_accounts"
LIST_XERO_TAX_RATES = "list_xero_tax_rates"
FIND_XERO_BILL = "find_xero_bill"
READ_XERO_BILL = "read_xero_bill"
CREATE_XERO_DRAFT_BILL = "create_xero_draft_bill"
UPDATE_XERO_DRAFT_BILL = "update_xero_draft_bill"
ATTACH_MAIL_DOCUMENT_TO_XERO_BILL = "attach_mail_document_to_xero_bill"
CAPTURE_SUPPLIER_INVOICE = "capture_supplier_invoice"
EXECUTE_XERO_BILL = "execute_xero_bill"
DELETE_XERO_DRAFT_BILL = "delete_xero_draft_bill"
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
    "account_mapping_invalid",
    "supporting_document_missing",
    "supporting_document_mismatch",
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

_DRAFT_PROPERTIES = {
    "contact_id": _STRING,
    "invoice_number": _STRING,
    "date": _STRING,
    "due_date": _STRING,
    "currency": _STRING,
    "reference": _STRING,
    "line_amount_types": _STRING,
    "expected_total": _STRING,
    "line_items": StructuredSchema(ValueKind.ARRAY, items=_LINE_ITEM),
}
_DRAFT_REQUIRED = tuple(_DRAFT_PROPERTIES)

_REQUIRED_ATTACHMENT = _object(
    {"filename": _STRING, "sha256": _STRING},
    ("filename", "sha256"),
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
    _object(_DRAFT_PROPERTIES, _DRAFT_REQUIRED),
    _BILL_RESULT,
    SideEffect.EFFECTFUL,
    _FAILURES,
)

UPDATE_DRAFT_DEFINITION = CapabilityDefinition(
    UPDATE_XERO_DRAFT_BILL,
    "Update one exact DRAFT accounts-payable bill from explicit, balanced structured values after verifying its current invoice number and total.",
    _object(
        {
            "invoice_id": _STRING,
            "expected_current_invoice_number": _STRING,
            "expected_current_total": _STRING,
            **_DRAFT_PROPERTIES,
        },
        (
            "invoice_id",
            "expected_current_invoice_number",
            "expected_current_total",
            *_DRAFT_REQUIRED,
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
            "attachment_id": _STRING,
            "filename": _STRING,
            "sha256": _STRING,
            "attached": _BOOLEAN,
        },
        ("invoice_id", "attachment_id", "filename", "sha256", "attached"),
    ),
    SideEffect.EFFECTFUL,
    _FAILURES,
)

_SOURCE_DOCUMENT = _object(
    {
        "mailbox_id": _STRING,
        "uid_validity": _STRING,
        "uid": _STRING,
        "attachment_id": _STRING,
        "expected_sha256": _STRING,
    },
    ("mailbox_id", "uid_validity", "uid", "attachment_id", "expected_sha256"),
)

CAPTURE_INVOICE_DEFINITION = CapabilityDefinition(
    CAPTURE_SUPPLIER_INVOICE,
    "Read one identified mail attachment as a supplier invoice, resolve its supplier and accounting treatment from this organisation's own records, and commit the bill when every one of those is unambiguous; otherwise return what is unresolved without acting.",
    _object(
        {
            "mailbox_id": _STRING,
            "uid_validity": _STRING,
            "uid": _STRING,
            "attachment_id": _STRING,
            "expected_sha256": _STRING,
            "context_line": _STRING,
            "authorise": _BOOLEAN,
        },
        (
            "mailbox_id",
            "uid_validity",
            "uid",
            "attachment_id",
            "expected_sha256",
            "authorise",
        ),
    ),
    _object(
        {
            "completed": _BOOLEAN,
            "returned_for": _STRING,
            "detail": _STRING,
            "invoice": _ANY_OBJECT,
            "bill": _BILL_RESULT,
            "attached": StructuredSchema(ValueKind.ARRAY, items=_STRING),
            "steps": StructuredSchema(ValueKind.ARRAY, items=_STRING),
        },
        ("completed", "returned_for", "detail", "invoice", "bill", "attached", "steps"),
    ),
    SideEffect.EFFECTFUL,
    _FAILURES + ("document_has_no_text", "not_an_invoice", "extraction_unverified"),
)

EXECUTE_BILL_DEFINITION = CapabilityDefinition(
    EXECUTE_XERO_BILL,
    "Commit one accounts-payable bill whose supplier, lines, amounts, account codes and tax types were already decided, then attach the named source documents, read it back and verify it. Returns without acting whenever the outcome is not objectively determined.",
    _object(
        {
            **_DRAFT_PROPERTIES,
            "source_documents": StructuredSchema(
                ValueKind.ARRAY, items=_SOURCE_DOCUMENT
            ),
            "authorise": _BOOLEAN,
        },
        (*_DRAFT_REQUIRED, "source_documents", "authorise"),
    ),
    _object(
        {
            "completed": _BOOLEAN,
            "returned_for": _STRING,
            "detail": _STRING,
            "bill": _BILL_RESULT,
            "attached": StructuredSchema(ValueKind.ARRAY, items=_STRING),
            "steps": StructuredSchema(ValueKind.ARRAY, items=_STRING),
        },
        ("completed", "returned_for", "detail", "bill", "attached", "steps"),
    ),
    SideEffect.EFFECTFUL,
    _FAILURES,
)

DELETE_DRAFT_DEFINITION = CapabilityDefinition(
    DELETE_XERO_DRAFT_BILL,
    "Discard one exact DRAFT accounts-payable bill after verifying its invoice number and total; refuse a bill that is not a draft.",
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

AUTHORISE_BILL_DEFINITION = CapabilityDefinition(
    AUTHORISE_XERO_BILL,
    "Authorise one exact DRAFT Xero bill only after its invoice number and total match the supplied expected values.",
    _object(
        {
            "invoice_id": _STRING,
            "invoice_number": _STRING,
            "expected_total": _STRING,
            "required_attachments": StructuredSchema(
                ValueKind.ARRAY, items=_REQUIRED_ATTACHMENT
            ),
        },
        (
            "invoice_id",
            "invoice_number",
            "expected_total",
            "required_attachments",
        ),
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
    UPDATE_DRAFT_DEFINITION,
    ATTACH_DOCUMENT_DEFINITION,
    CAPTURE_INVOICE_DEFINITION,
    EXECUTE_BILL_DEFINITION,
    DELETE_DRAFT_DEFINITION,
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
    if value is None or isinstance(value, bool):
        raise XeroAccessError("response_invalid")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise XeroAccessError("response_invalid") from None
    if not parsed.is_finite():
        raise XeroAccessError("response_invalid")
    return format(parsed.quantize(Decimal("0.01")), "f")


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
    invoice_id = str(bill.get("InvoiceID") or "")
    invoice_number = str(bill.get("InvoiceNumber") or "")
    contact_id = str(contact.get("ContactID") or "")
    status = str(bill.get("Status") or "")
    has_attachments = bill.get("HasAttachments")
    if (
        not invoice_id
        or not invoice_number
        or not contact_id
        or not status
        or not isinstance(has_attachments, bool)
    ):
        raise XeroAccessError("response_invalid")
    return {
        "found": True,
        "invoice_id": invoice_id,
        "invoice_number": invoice_number,
        "contact_id": contact_id,
        "contact_name": str(contact.get("Name") or ""),
        "status": status,
        "currency": str(bill.get("CurrencyCode") or ""),
        "total": _money(bill.get("Total")),
        "amount_due": _money(bill.get("AmountDue")),
        "reference": str(bill.get("Reference") or ""),
        "has_attachments": has_attachments,
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
        if quantity <= 0 or tax_amount < 0:
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


def _decimal_or_zero(value: str) -> Decimal:
    try:
        return Decimal(value or "0")
    except InvalidOperation:
        return Decimal("0")


def _validate_line_mappings(
    account: XeroAccountingAccount,
    lines: Sequence[Mapping[str, Any]],
    line_amount_types: str,
) -> None:
    """Fail before a write when a code/tax pair is not live in this tenant."""
    active_accounts: dict[str, Mapping[str, Any]] = {}
    for item in account.list_accounts():
        code = str(item.get("Code") or "")
        if code and str(item.get("Status") or "") == "ACTIVE":
            if code in active_accounts:
                raise XeroAccessError("account_mapping_invalid")
            active_accounts[code] = item
    active_taxes = {
        str(item.get("TaxType") or "")
        for item in account.list_tax_rates()
        if str(item.get("Status") or "") == "ACTIVE"
    }
    for line in lines:
        code = str(line.get("AccountCode") or "")
        tax_type = str(line.get("TaxType") or "")
        configured = active_accounts.get(code)
        if configured is None:
            raise XeroAccessError("account_mapping_invalid")
        default_tax = str(configured.get("TaxType") or "")
        if line_amount_types == "NoTax":
            valid_pair = tax_type == "NONE"
        else:
            # An account's tax type is Xero's default for that account, not a
            # constraint on it. A supplier who charges no VAT is ordinary, so
            # NONE stays valid on a VAT-defaulted account; any other override
            # must still be a tax type that is live in this organisation.
            valid_pair = tax_type == "NONE" or tax_type in active_taxes
        if not valid_pair or (tax_type != "NONE" and tax_type not in active_taxes):
            raise XeroAccessError("account_mapping_invalid")


def _draft_payload(
    arguments: StructuredData,
    account: XeroAccountingAccount,
) -> tuple[dict[str, Any], Decimal]:
    contact_id = _required(arguments, "contact_id")
    invoice_number = _required(arguments, "invoice_number")
    issue_date = _required(arguments, "date")
    due_date = _required(arguments, "due_date")
    date.fromisoformat(issue_date)
    date.fromisoformat(due_date)
    expected_total = _decimal(arguments.get("expected_total"), "expected_total")
    if expected_total <= 0:
        raise ValueError("expected_total")
    lines, calculated_total = _line_items(arguments)
    if calculated_total != expected_total.quantize(Decimal("0.01")):
        raise ValueError("expected_total")
    line_amount_types = _required(arguments, "line_amount_types")
    _validate_line_mappings(account, lines, line_amount_types)
    return (
        {
            "Type": "ACCPAY",
            "Contact": {"ContactID": contact_id},
            "InvoiceNumber": invoice_number,
            "Date": issue_date,
            "DueDate": due_date,
            "CurrencyCode": _required(arguments, "currency"),
            "Reference": _required(arguments, "reference"),
            "LineAmountTypes": line_amount_types,
            "LineItems": lines,
            "Status": "DRAFT",
        },
        expected_total.quantize(Decimal("0.01")),
    )


def _sha256(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(name)
    digest = value.strip().lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(name)
    return digest


def _required_attachment_values(
    arguments: StructuredData,
) -> tuple[tuple[str, str], ...]:
    values = arguments.get("required_attachments")
    if not isinstance(values, (tuple, list)) or not values:
        raise ValueError("required_attachments")
    found: list[tuple[str, str]] = []
    for item in values:
        if not isinstance(item, Mapping):
            raise ValueError("required_attachments")
        filename = _required(item, "filename")
        digest = _sha256(item.get("sha256"), "sha256")
        if (filename, digest) in found:
            raise ValueError("required_attachments")
        found.append((filename, digest))
    return tuple(found)


def _verified_attachment(
    account: XeroAccountingAccount,
    invoice_id: str,
    filename: str,
    digest: str,
) -> Mapping[str, Any]:
    candidates = tuple(
        item
        for item in account.list_bill_attachments(invoice_id)
        if str(item.get("FileName") or "") == filename
    )
    for item in candidates:
        attachment_id = str(item.get("AttachmentID") or "")
        media_type = str(item.get("MimeType") or "")
        if not attachment_id or not media_type:
            continue
        payload = account.read_bill_attachment(invoice_id, attachment_id, media_type)
        if hashlib.sha256(payload).hexdigest() == digest:
            return item
    raise XeroAccessError(
        "supporting_document_missing" if not candidates else "supporting_document_mismatch"
    )


def build_xero_executors(
    account: XeroAccountingAccount,
    mail: MailAccount,
    call_id_source: Callable[[], str],
    extractor: Callable[[str, str], Mapping[str, Any]] | None = None,
    default_account_code: str = "",
    default_tax_type: str = "",
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
            bill, expected_total = _draft_payload(arguments, account)
            contact_id = str(bill["Contact"]["ContactID"])
            invoice_number = str(bill["InvoiceNumber"])
            if account.find_bill(invoice_number, contact_id) is not None:
                raise XeroAccessError("duplicate_found")
            created = account.create_draft_bill(bill)
            values = _bill_values(created)
            if (
                values["invoice_number"] != invoice_number
                or values["contact_id"] != contact_id
                or values["status"] != "DRAFT"
                or Decimal(values["total"]) != expected_total
            ):
                raise XeroAccessError("source_mismatch")
            return values

        return invoke(CREATE_XERO_DRAFT_BILL, operation)

    def update_draft(arguments: StructuredData) -> CapabilityResult:
        def operation() -> Mapping[str, Any]:
            invoice_id = _required(arguments, "invoice_id")
            expected_current_number = _required(
                arguments, "expected_current_invoice_number"
            )
            expected_current_total = _decimal(
                arguments.get("expected_current_total"), "expected_current_total"
            ).quantize(Decimal("0.01"))
            current = _bill_values(account.read_bill(invoice_id))
            if not current["found"]:
                raise XeroAccessError("bill_not_found")
            if current["status"] != "DRAFT":
                raise XeroAccessError("bill_not_draft")
            if (
                current["invoice_number"] != expected_current_number
                or Decimal(current["total"]) != expected_current_total
            ):
                raise XeroAccessError("source_mismatch")
            bill, expected_total = _draft_payload(arguments, account)
            contact_id = str(bill["Contact"]["ContactID"])
            invoice_number = str(bill["InvoiceNumber"])
            duplicate = account.find_bill(invoice_number, contact_id)
            if duplicate is not None and str(duplicate.get("InvoiceID") or "") != invoice_id:
                raise XeroAccessError("duplicate_found")
            updated = _bill_values(account.update_draft_bill(invoice_id, bill))
            if (
                updated["invoice_id"] != invoice_id
                or updated["invoice_number"] != invoice_number
                or updated["contact_id"] != contact_id
                or updated["status"] != "DRAFT"
                or Decimal(updated["total"]) != expected_total
            ):
                raise XeroAccessError("source_mismatch")
            return updated

        return invoke(UPDATE_XERO_DRAFT_BILL, operation)

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
            expected_sha256 = _sha256(
                arguments.get("expected_sha256"), "expected_sha256"
            )
            if attachment.sha256 != expected_sha256:
                return failed(ATTACH_MAIL_DOCUMENT_TO_XERO_BILL, "source_mismatch")
            account.attach_bill_document(
                invoice_id,
                attachment.filename,
                attachment.media_type,
                content,
            )
            verified = _verified_attachment(
                account, invoice_id, attachment.filename, expected_sha256
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
                "attachment_id": str(verified.get("AttachmentID") or ""),
                "filename": attachment.filename,
                "sha256": attachment.sha256,
                "attached": True,
            },
        )

    def execute_bill(arguments: StructuredData) -> CapabilityResult:
        """Commit values AL/X already decided; return whenever judgment is needed.

        Every step here is mechanical: the supplier, lines, amounts, account
        codes and tax types arrive already chosen. Nothing in this function
        selects accounting meaning, and any condition without one objectively
        correct outcome stops and returns to AL/X.
        """
        steps: list[str] = []
        attached: list[str] = []

        def returned(reason: str, detail: str, bill: Mapping[str, Any] | None = None):
            return CapabilityResult(
                call_id_source(),
                EXECUTE_XERO_BILL,
                CapabilityResultState.SUCCEEDED,
                {
                    "completed": False,
                    "returned_for": reason,
                    "detail": detail,
                    "bill": _bill_values(bill),
                    "attached": tuple(attached),
                    "steps": tuple(steps),
                },
            )

        try:
            documents = arguments.get("source_documents")
            if not isinstance(documents, (tuple, list)) or not documents:
                raise ValueError("source_documents")
            authorise_requested = arguments.get("authorise")
            if not isinstance(authorise_requested, bool):
                raise ValueError("authorise")
            bill_payload, expected_total = _draft_payload(arguments, account)
            contact_id = str(bill_payload["Contact"]["ContactID"])
            invoice_number = str(bill_payload["InvoiceNumber"])
            steps.append("validated_supplied_values")

            # Idempotency. A prior attempt may have created this bill before
            # failing, so an existing draft for the same supplier and invoice
            # number is resumed rather than duplicated. Anything already beyond
            # draft is an outcome only AL/X may judge.
            existing = account.find_bill(invoice_number, contact_id)
            current = _bill_values(existing)
            if current["found"]:
                if current["status"] != "DRAFT":
                    return returned(
                        "duplicate_bill",
                        f"a {current['status']} bill already exists for {invoice_number}",
                        existing,
                    )
                if Decimal(current["total"]) != expected_total:
                    return returned(
                        "duplicate_bill",
                        f"an existing draft for {invoice_number} totals "
                        f"{current['total']}, not {expected_total}",
                        existing,
                    )
                invoice_id = current["invoice_id"]
                steps.append("resumed_existing_draft")
            else:
                created = _bill_values(account.create_draft_bill(bill_payload))
                if (
                    created["invoice_number"] != invoice_number
                    or Decimal(created["total"]) != expected_total
                ):
                    raise XeroAccessError("source_mismatch")
                invoice_id = created["invoice_id"]
                steps.append("created_draft")

            stored = {
                str(item.get("FileName") or "")
                for item in account.list_bill_attachments(invoice_id)
            }
            for document in documents:
                if not isinstance(document, Mapping):
                    raise ValueError("source_documents")
                reference = MailReference(
                    _required(document, "mailbox_id"),
                    _required(document, "uid_validity"),
                    _required(document, "uid"),
                )
                digest = _sha256(document.get("expected_sha256"), "expected_sha256")
                attachment, content = mail.read_attachment(
                    reference, _required(document, "attachment_id")
                )
                if attachment.sha256 != digest:
                    return returned(
                        "attachment_mismatch",
                        f"{attachment.filename} does not match the approved digest",
                        account.read_bill(invoice_id),
                    )
                # Re-attaching the same filename would duplicate it on retry.
                if attachment.filename not in stored:
                    account.attach_bill_document(
                        invoice_id, attachment.filename, attachment.media_type, content
                    )
                    stored.add(attachment.filename)
                _verified_attachment(
                    account, invoice_id, attachment.filename, digest
                )
                attached.append(attachment.filename)
            steps.append("attached_and_verified_documents")

            if authorise_requested:
                account.authorise_bill(invoice_id)
                steps.append("authorised")

            final = _bill_values(account.read_bill(invoice_id))
            steps.append("read_back")
            expected_status = "AUTHORISED" if authorise_requested else "DRAFT"
            if (
                not final["found"]
                or final["invoice_id"] != invoice_id
                or final["invoice_number"] != invoice_number
                or final["contact_id"] != contact_id
                or Decimal(final["total"]) != expected_total
                or final["status"] != expected_status
                or not final["has_attachments"]
            ):
                return returned(
                    "read_back_mismatch",
                    "the committed bill does not match the requested values",
                    account.read_bill(invoice_id),
                )
            steps.append("verified")
            return CapabilityResult(
                call_id_source(),
                EXECUTE_XERO_BILL,
                CapabilityResultState.SUCCEEDED,
                {
                    "completed": True,
                    "returned_for": "",
                    "detail": "",
                    "bill": final,
                    "attached": tuple(attached),
                    "steps": tuple(steps),
                },
            )
        except ValueError:
            return failed(EXECUTE_XERO_BILL, "arguments_unusable")
        except MailAccessError as error:
            return failed(EXECUTE_XERO_BILL, error.code)
        except XeroAccessError as error:
            # account_mapping_invalid means AL/X supplied an account or tax type
            # this organisation does not have. Choosing a replacement would be
            # deciding accounting meaning, so it returns to her instead.
            if error.code == "account_mapping_invalid":
                return returned(
                    "account_or_tax_unresolved",
                    "a supplied account code or tax type is not active in this organisation",
                )
            return failed(EXECUTE_XERO_BILL, error.code)

    def capture_invoice(arguments: StructuredData) -> CapabilityResult:
        """Read a document, resolve it against records, and commit if certain.

        Extraction is a bounded specialist question, supplier and accounting
        treatment come from this organisation's own history, and the commit is
        the same execute path. Nothing here interprets what Friedl wants, and
        anything without one objectively correct answer returns to AL/X rather
        than being guessed.
        """
        steps: list[str] = []

        def returned(reason: str, detail: str, invoice: Mapping[str, Any] | None = None):
            return CapabilityResult(
                call_id_source(),
                CAPTURE_SUPPLIER_INVOICE,
                CapabilityResultState.SUCCEEDED,
                {
                    "completed": False,
                    "returned_for": reason,
                    "detail": detail,
                    "invoice": dict(invoice or {}),
                    "bill": _bill_values(None),
                    "attached": (),
                    "steps": tuple(steps),
                },
            )

        if extractor is None:
            return failed(CAPTURE_SUPPLIER_INVOICE, "arguments_unusable")
        try:
            reference = MailReference(
                _required(arguments, "mailbox_id"),
                _required(arguments, "uid_validity"),
                _required(arguments, "uid"),
            )
            digest = _sha256(arguments.get("expected_sha256"), "expected_sha256")
            authorise_requested = arguments.get("authorise")
            if not isinstance(authorise_requested, bool):
                raise ValueError("authorise")
            context_line = arguments.get("context_line", "")
            if not isinstance(context_line, str):
                raise ValueError("context_line")
            attachment, _content = mail.read_attachment(
                reference, _required(arguments, "attachment_id")
            )
            if attachment.sha256 != digest:
                return failed(CAPTURE_SUPPLIER_INVOICE, "source_mismatch")
            steps.append("read_source_document")

            invoice = extractor(attachment.text, context_line)
            steps.append("extracted_invoice")
            if not is_supplier_bill(invoice["document_type"]):
                return failed(CAPTURE_SUPPLIER_INVOICE, "not_an_invoice")
            if not invoice["verified"]:
                return returned(
                    "extraction_unverified",
                    "; ".join(invoice["problems"]),
                    invoice,
                )

            contact = resolve_supplier(
                account.search_contacts(invoice["supplier_name"]),
                invoice["supplier_name"],
            )
            steps.append("resolved_supplier")
            if not contact["resolved"]:
                return returned("supplier_unresolved", contact["reason"], invoice)

            history = account.bills_for_contact(contact["contact_id"])
            # The document itself settles the tax question: a supplier that
            # charges VAT shows it, one that does not cannot be claimed for.
            shows_tax = _decimal_or_zero(invoice["tax_amount"]) > 0
            coding = prior_coding(
                history, default_account_code, shows_tax, default_tax_type
            )
            steps.append("looked_up_prior_coding")
            if not coding["resolved"]:
                return returned("coding_unresolved", coding["reason"], invoice)
        except ValueError:
            return failed(CAPTURE_SUPPLIER_INVOICE, "arguments_unusable")
        except MailAccessError as error:
            return failed(CAPTURE_SUPPLIER_INVOICE, error.code)
        except SpecialistError as error:
            return failed(CAPTURE_SUPPLIER_INVOICE, error.code)
        except XeroAccessError as error:
            return failed(CAPTURE_SUPPLIER_INVOICE, error.code)

        # Everything is unambiguous, so the same commit path runs directly
        # rather than returning to AL/X to be told to do what is already known.
        outcome = execute_bill(
            {
                "contact_id": contact["contact_id"],
                "invoice_number": invoice["invoice_number"],
                "date": invoice["invoice_date"],
                "due_date": invoice["due_date"] or invoice["invoice_date"],
                "currency": invoice["currency"],
                "reference": context_line or attachment.filename,
                "line_amount_types": coding["line_amount_types"] or "NoTax",
                "expected_total": invoice["total"],
                "line_items": [
                    {
                        "description": invoice["description"] or "Supplier invoice",
                        "quantity": "1",
                        "unit_amount": invoice["subtotal"] or invoice["total"],
                        "account_code": coding["account_code"],
                        "tax_type": coding["tax_type"],
                        "tax_amount": invoice["tax_amount"] or "0",
                    }
                ],
                "source_documents": [dict(arguments)],
                "authorise": authorise_requested,
            }
        )
        steps.append("executed_bill")
        if outcome.state is not CapabilityResultState.SUCCEEDED:
            return CapabilityResult(
                call_id_source(),
                CAPTURE_SUPPLIER_INVOICE,
                CapabilityResultState.FAILED,
                failure=dict(outcome.failure or {"code": "request_rejected"}),
            )
        return CapabilityResult(
            call_id_source(),
            CAPTURE_SUPPLIER_INVOICE,
            CapabilityResultState.SUCCEEDED,
            {
                **outcome.values,
                "invoice": invoice,
                "steps": (*steps, *outcome.values["steps"]),
            },
        )

    def delete_draft(arguments: StructuredData) -> CapabilityResult:
        def operation() -> Mapping[str, Any]:
            invoice_id = _required(arguments, "invoice_id")
            invoice_number = _required(arguments, "invoice_number")
            expected_total = _decimal(arguments.get("expected_total"), "expected_total")
            current = _bill_values(account.read_bill(invoice_id))
            if not current["found"]:
                raise XeroAccessError("bill_not_found")
            # Only a draft may be discarded. An authorised bill is an accounting
            # entry whose reversal is not covered by this capability.
            if current["status"] not in ("DRAFT", "SUBMITTED"):
                raise XeroAccessError("bill_not_draft")
            if (
                current["invoice_number"] != invoice_number
                or Decimal(current["total"]) != expected_total
            ):
                raise XeroAccessError("source_mismatch")
            deleted = _bill_values(account.delete_draft_bill(invoice_id))
            if (
                deleted["invoice_id"] != invoice_id
                or deleted["status"] != "DELETED"
            ):
                raise XeroAccessError("source_mismatch")
            return deleted

        return invoke(DELETE_XERO_DRAFT_BILL, operation)

    def authorise(arguments: StructuredData) -> CapabilityResult:
        def operation() -> Mapping[str, Any]:
            invoice_id = _required(arguments, "invoice_id")
            invoice_number = _required(arguments, "invoice_number")
            expected_total = _decimal(arguments.get("expected_total"), "expected_total")
            required_attachments = _required_attachment_values(arguments)
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
            for filename, digest in required_attachments:
                _verified_attachment(account, invoice_id, filename, digest)
            authorised = _bill_values(account.authorise_bill(invoice_id))
            if (
                authorised["invoice_id"] != invoice_id
                or authorised["invoice_number"] != invoice_number
                or authorised["status"] != "AUTHORISED"
                or Decimal(authorised["total"]) != expected_total
                or not authorised["has_attachments"]
            ):
                raise XeroAccessError("source_mismatch")
            return authorised

        return invoke(AUTHORISE_XERO_BILL, operation)

    return {
        SEARCH_XERO_CONTACTS: search_contacts,
        LIST_XERO_ACCOUNTS: list_accounts,
        LIST_XERO_TAX_RATES: list_tax_rates,
        FIND_XERO_BILL: find_bill,
        READ_XERO_BILL: read_bill,
        CREATE_XERO_DRAFT_BILL: create_draft,
        UPDATE_XERO_DRAFT_BILL: update_draft,
        ATTACH_MAIL_DOCUMENT_TO_XERO_BILL: attach_document,
        CAPTURE_SUPPLIER_INVOICE: capture_invoice,
        EXECUTE_XERO_BILL: execute_bill,
        DELETE_XERO_DRAFT_BILL: delete_draft,
        AUTHORISE_XERO_BILL: authorise,
    }
