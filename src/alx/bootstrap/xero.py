"""Compose the approved Xero supplier-bill primitives into AL/X boundaries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from alx.config import LlamaParseSettings, XeroSettings
from alx.contracts import CapabilityDefinition, CapabilityResult, MailAccount, StructuredData
from alx.providers import LlamaParseInvoiceExtractor, SQLiteXeroOAuth, XeroAccountingAdapter
from alx.providers.dhl import classify_dhl_document
from alx.safety import AuthorityPolicy
from alx.specialists import ANSWER_SCHEMA, INSTRUCTION, checked_invoice
from alx.tools import (
    PROCESS_DHL_IMPORT,
    CAPTURE_SUPPLIER_INVOICE,
    DELETE_XERO_DRAFT_BILL,
    FIND_XERO_BILL,
    LIST_XERO_ACCOUNTS,
    LIST_XERO_TAX_RATES,
    READ_XERO_BILL,
    SEARCH_XERO_CONTACTS,
    UPDATE_XERO_CONTACT,
    CREATE_XERO_CONTACT,
    FIND_XERO_QUOTES,
    INVOICE_XERO_QUOTE,
    XERO_DEFINITIONS,
    build_xero_executors,
)


XERO_READ_PERMISSION = "xero.read"
XERO_BILL_WRITE_PERMISSION = "xero.bill.write"
XERO_BILL_DELETE_PERMISSION = "xero.bill.delete"
XERO_CONTACT_RENAME_PERMISSION = "xero.contact.rename"
XERO_CONTACT_CREATE_PERMISSION = "xero.contact.create"
XERO_QUOTE_INVOICE_PERMISSION = "xero.quote.invoice"

# Law 0: one production path per outcome. An ordinary supplier bill is posted
# by capture_supplier_invoice and a DHL import by process_dhl_import. The steps
# inside each are private implementation, not competing entry points, and the
# capabilities they replaced are deleted rather than withheld.
# Both commit a bill, so both close their own ceiling window when they
# actually finish. A DHL import armed the ceiling but could never settle it,
# so a completed import left the conversation inside a spent window.
BILL_EXECUTION_CAPABILITIES = frozenset(
    {CAPTURE_SUPPLIER_INVOICE, PROCESS_DHL_IMPORT}
)

# Arming the ceiling on the commit was too late: a task spent seven reasoning
# calls reaching Xero and stayed unbudgeted because it never got that far. Any
# of these says bill processing has begun, so the ceiling applies from the
# first one AL/X reaches for.
BILL_TASK_CAPABILITIES = BILL_EXECUTION_CAPABILITIES | {
    PROCESS_DHL_IMPORT,
    SEARCH_XERO_CONTACTS,
    LIST_XERO_ACCOUNTS,
    LIST_XERO_TAX_RATES,
    FIND_XERO_BILL,
    READ_XERO_BILL,
    DELETE_XERO_DRAFT_BILL,
}


def build_supplier_invoice_extractor(
    settings: LlamaParseSettings,
    *,
    client: Any = None,
) -> Callable[[bytes, str, str, str], Mapping[str, Any]] | None:
    """Wire LlamaParse into the capture extractor, or None when unusable.

    Capture is advertised only when this returns an extractor. Missing
    configuration must not fall back to the Core or the generic specialist.
    """
    if not settings.is_usable:
        return None
    adapter = LlamaParseInvoiceExtractor(
        settings.api_key,
        settings.base_url,
        settings.timeout_seconds,
        settings.project_id,
        ANSWER_SCHEMA,
        INSTRUCTION,
        client,
    )

    def extract(
        payload: bytes, media_type: str, filename: str, context_line: str
    ) -> Mapping[str, Any]:
        return checked_invoice(
            adapter.extract(payload, media_type, filename, context_line)
        )

    return extract


@dataclass(frozen=True, slots=True)
class XeroRuntime:
    oauth: SQLiteXeroOAuth
    adapter: XeroAccountingAdapter
    definitions: tuple[CapabilityDefinition, ...]
    policies: Mapping[str, AuthorityPolicy]
    executors: Mapping[str, Callable[[StructuredData], CapabilityResult]]
    permissions: frozenset[str]


def build_xero_runtime(
    settings: XeroSettings,
    storage_root: Path,
    mail_account: MailAccount,
    call_id_source: Callable[[], str],
    extractor: Callable[[bytes, str, str, str], Mapping[str, Any]] | None = None,
) -> XeroRuntime:
    oauth = SQLiteXeroOAuth(
        storage_root / "xero.sqlite3",
        settings.client_id,
        settings.client_secret,
        settings.redirect_uri,
        settings.tenant_id,
        settings.timeout_seconds,
    )
    adapter = XeroAccountingAdapter(oauth, settings.timeout_seconds)
    read_policy = AuthorityPolicy(frozenset({XERO_READ_PERMISSION}))
    # D-018. Friedl weighed the risk of an incorrect bill against re-proving
    # carried-over V1 behaviour and authorised unattended supplier-bill writes
    # of any amount. A bill is not a payment and is reversible in Xero. The
    # structural safeguards are unchanged: balanced lines, account and tax
    # identifiers validated against the live organisation, duplicate refusal,
    # hash-bound attachments verified byte-for-byte, and read-back after every
    # write. Payment and bank scopes remain unrequested.
    write_policy = AuthorityPolicy(
        frozenset({XERO_BILL_WRITE_PERMISSION}),
        approval_required=not settings.unattended_bill_writes,
    )
    # D-019. Discarding a bill is a different act from preparing one, so it
    # carries its own permission and its own approval setting. Friedl scoped
    # this to drafts; voiding an authorised bill is not authorised here.
    delete_policy = AuthorityPolicy(
        frozenset({XERO_BILL_DELETE_PERMISSION}),
        approval_required=not settings.unattended_bill_deletes,
    )
    # D-034. Friedl granted standing authority to rename one existing contact
    # by exact ContactID. It is its own permission because it writes a
    # different record from a bill, and no setting makes it attended.
    rename_policy = AuthorityPolicy(frozenset({XERO_CONTACT_RENAME_PERMISSION}))
    # D-035. Standing authority to create one supplier contact. Creating is a
    # different act from renaming, so neither permission carries the other.
    create_policy = AuthorityPolicy(frozenset({XERO_CONTACT_CREATE_PERMISSION}))
    # D-037. Standing authority to turn one sent or accepted quote into a
    # DRAFT sales invoice for the PO that accepts it. A draft is not sent,
    # approved or paid, so no setting makes it attended. Its own permission:
    # a sales document is a different record from a supplier bill.
    quote_invoice_policy = AuthorityPolicy(
        frozenset({XERO_QUOTE_INVOICE_PERMISSION})
    )
    policies = {
        SEARCH_XERO_CONTACTS: read_policy,
        LIST_XERO_ACCOUNTS: read_policy,
        LIST_XERO_TAX_RATES: read_policy,
        FIND_XERO_BILL: read_policy,
        READ_XERO_BILL: read_policy,
        DELETE_XERO_DRAFT_BILL: delete_policy,
        UPDATE_XERO_CONTACT: rename_policy,
        CREATE_XERO_CONTACT: create_policy,
        FIND_XERO_QUOTES: read_policy,
        INVOICE_XERO_QUOTE: quote_invoice_policy,
    }
    definitions = tuple(
        definition
        for definition in XERO_DEFINITIONS
        if extractor is not None
        or definition.capability_id != CAPTURE_SUPPLIER_INVOICE
    )
    executors = dict(
        build_xero_executors(
            adapter,
            mail_account,
            call_id_source,
            extractor,
            settings.default_account_code,
            settings.default_tax_type,
            classify_dhl_document,
        )
    )
    if extractor is not None:
        policies[CAPTURE_SUPPLIER_INVOICE] = write_policy
    else:
        # The catalogue promises that each offered capability is callable in
        # this runtime. Supplier capture cannot read a document without its
        # LlamaParse extractor, so it is absent rather than advertised as a
        # callable that will fail before doing any work.
        executors.pop(CAPTURE_SUPPLIER_INVOICE)
    return XeroRuntime(
        oauth,
        adapter,
        definitions,
        policies,
        executors,
        frozenset(
            {
                XERO_READ_PERMISSION,
                XERO_BILL_WRITE_PERMISSION,
                XERO_BILL_DELETE_PERMISSION,
                XERO_CONTACT_RENAME_PERMISSION,
                XERO_CONTACT_CREATE_PERMISSION,
                XERO_QUOTE_INVOICE_PERMISSION,
            }
        ),
    )
