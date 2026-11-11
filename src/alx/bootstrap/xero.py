"""Compose the one `xero` accounting capability into AL/X boundaries (D-074)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from alx.config import LlamaParseSettings, XeroSettings
from alx.contracts import (
    CapabilityDefinition,
    CapabilityResult,
    MailAccount,
    StructuredData,
    XeroAction,
)
from alx.providers import LlamaParseExtractor, SQLiteXeroOAuth, XeroAccountingAdapter
from alx.providers.dhl import classify_dhl_document
from alx.safety import AuthorityPolicy
from alx.specialists import ANSWER_SCHEMA, INSTRUCTION, checked_invoice
from alx.tools import XERO, build_xero_executors, offered_xero_actions, xero_definition
from alx.bootstrap.dhl import DhlImport
from alx.bootstrap.digikey import DIGIKEY_ORDER_READ_PERMISSION


XERO_READ_PERMISSION = "xero.read"
XERO_BILL_WRITE_PERMISSION = "xero.bill.write"
XERO_BILL_DELETE_PERMISSION = "xero.bill.delete"
XERO_CONTACT_RENAME_PERMISSION = "xero.contact.rename"
XERO_CONTACT_CREATE_PERMISSION = "xero.contact.create"
XERO_QUOTE_INVOICE_PERMISSION = "xero.quote.invoice"

# Law 0: one production path per outcome. An ordinary supplier bill is posted
# through the shared structured-invoice core (mail/PDF or DigiKey source), and
# a DHL import by import_dhl. The steps inside each are private
# implementation, not competing writers. Superseded capabilities are deleted.
# Both commit a bill, so both close their own ceiling window when they
# actually finish. A DHL import armed the ceiling but could never settle it,
# so a completed import left the conversation inside a spent window.
BILL_EXECUTION_ACTIONS = frozenset(
    {XeroAction.CAPTURE_INVOICE, XeroAction.CAPTURE_DIGIKEY_BILL, XeroAction.IMPORT_DHL}
)

# Arming the ceiling on the commit was too late: a task spent seven reasoning
# calls reaching Xero and stayed unbudgeted because it never got that far. Any
# of these says bill processing has begun, so the ceiling applies from the
# first one AL/X reaches for.
BILL_TASK_ACTIONS = BILL_EXECUTION_ACTIONS | {
    XeroAction.SEARCH_CONTACTS,
    XeroAction.LIST_ACCOUNTS,
    XeroAction.LIST_TAX_RATES,
    XeroAction.FIND_BILL,
    XeroAction.READ_BILL,
    XeroAction.DELETE_DRAFT_BILL,
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
    adapter = LlamaParseExtractor(
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
    # The actions of `xero` this configuration offers (D-074).
    actions: tuple[XeroAction, ...] = ()


def build_xero_runtime(
    settings: XeroSettings,
    storage_root: Path,
    mail_account: MailAccount,
    call_id_source: Callable[[], str],
    extractor: Callable[[bytes, str, str, str], Mapping[str, Any]] | None = None,
    digikey_lookup: Callable[..., Mapping[str, Any]] | None = None,
    dhl: DhlImport | None = None,
) -> XeroRuntime:
    """Build the one `xero` capability (D-074) with the actions configured here."""
    oauth = SQLiteXeroOAuth(
        storage_root / "xero.sqlite3",
        settings.client_id,
        settings.client_secret,
        settings.redirect_uri,
        settings.tenant_id,
        settings.timeout_seconds,
    )
    adapter = XeroAccountingAdapter(oauth, settings.timeout_seconds)
    # Each action keeps the permission it had as its own capability; the one
    # `xero` policy holds the union of those of the offered actions.
    permission_of = {
        XeroAction.SEARCH_CONTACTS: XERO_READ_PERMISSION,
        XeroAction.LIST_ACCOUNTS: XERO_READ_PERMISSION,
        XeroAction.LIST_TAX_RATES: XERO_READ_PERMISSION,
        XeroAction.FIND_BILL: XERO_READ_PERMISSION,
        XeroAction.READ_BILL: XERO_READ_PERMISSION,
        XeroAction.FIND_QUOTES: XERO_READ_PERMISSION,
        XeroAction.LOOKUP_DIGIKEY: DIGIKEY_ORDER_READ_PERMISSION,
        # D-018. Friedl weighed the risk of an incorrect bill against
        # re-proving carried-over V1 behaviour and authorised unattended
        # supplier-bill writes of any amount. A bill is not a payment and is
        # reversible in Xero. The structural safeguards are unchanged:
        # balanced lines, account and tax identifiers validated against the
        # live organisation, duplicate refusal, hash-bound attachments
        # verified byte-for-byte, and read-back after every write. Payment and
        # bank scopes remain unrequested.
        XeroAction.CAPTURE_INVOICE: XERO_BILL_WRITE_PERMISSION,
        XeroAction.CAPTURE_DIGIKEY_BILL: XERO_BILL_WRITE_PERMISSION,
        # D-019. Discarding a bill is a different act from preparing one, so
        # it carries its own permission and its own approval setting. Friedl
        # scoped this to drafts; voiding an authorised bill is not authorised.
        XeroAction.DELETE_DRAFT_BILL: XERO_BILL_DELETE_PERMISSION,
        # D-034. Standing authority to rename one existing contact by exact
        # ContactID; it writes a different record from a bill, and no setting
        # makes it attended.
        XeroAction.UPDATE_CONTACT: XERO_CONTACT_RENAME_PERMISSION,
        # D-035. Standing authority to create one supplier contact. Creating
        # is a different act from renaming, so neither permission carries the
        # other.
        XeroAction.CREATE_CONTACT: XERO_CONTACT_CREATE_PERMISSION,
        # D-037. Standing authority to turn one sent or accepted quote into a
        # DRAFT sales invoice for the PO that accepts it. A draft is not sent,
        # approved or paid, so no setting makes it attended.
        XeroAction.INVOICE_QUOTE: XERO_QUOTE_INVOICE_PERMISSION,
    }
    # Which actions need Friedl's approval of the exact call follows his
    # settings for that kind of write, action by action, as it did when each
    # was its own capability.
    needs_approval = set()
    if not settings.unattended_bill_writes:
        needs_approval.update((XeroAction.CAPTURE_INVOICE, XeroAction.CAPTURE_DIGIKEY_BILL))
    if not settings.unattended_bill_deletes:
        needs_approval.add(XeroAction.DELETE_DRAFT_BILL)
    if dhl is not None:
        # D-021: the DHL import keeps its own permission and approval setting.
        permission_of[XeroAction.IMPORT_DHL] = dhl.permission
        if dhl.approval_required:
            needs_approval.add(XeroAction.IMPORT_DHL)
    executors = build_xero_executors(
        adapter,
        mail_account,
        call_id_source,
        extractor,
        settings.default_account_code,
        settings.default_tax_type,
        classify_dhl_document,
        digikey_lookup=digikey_lookup,
        # An approval of capture covers writes only. Cleanup may use
        # deletion's standing authority, never capture's approval.
        draft_cleanup_permitted=XeroAction.DELETE_DRAFT_BILL not in needs_approval,
        dhl_import=None if dhl is None else dhl.executor_for(adapter),
    )
    # The catalogue promises that each offered action is callable in this
    # runtime. Capture cannot read a document without its LlamaParse
    # extractor, the DigiKey actions need the DigiKey order read, and the DHL
    # import its own composition, so each is absent rather than advertised as
    # a call that will fail before doing any work.
    offered = offered_xero_actions(
        capture=extractor is not None, digikey=digikey_lookup is not None, dhl=dhl is not None,
    )
    permissions = frozenset(permission_of[item] for item in offered)
    policy = AuthorityPolicy(
        permissions,
        approval_required=True,
        approval_exempt_when=tuple(
            (("action", item.value),) for item in offered if item not in needs_approval
        ),
    )
    return XeroRuntime(
        oauth,
        adapter,
        (xero_definition(offered),),
        {XERO: policy},
        executors,
        permissions,
        offered,
    )
