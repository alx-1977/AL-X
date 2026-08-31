"""Compose the approved Xero supplier-bill primitives into AL/X boundaries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from alx.config import XeroSettings
from alx.contracts import CapabilityDefinition, CapabilityResult, MailAccount, StructuredData
from alx.providers import SQLiteXeroOAuth, XeroAccountingAdapter
from alx.safety import AuthorityPolicy
from alx.tools import (
    ATTACH_MAIL_DOCUMENT_TO_XERO_BILL,
    AUTHORISE_XERO_BILL,
    CREATE_XERO_DRAFT_BILL,
    FIND_XERO_BILL,
    LIST_XERO_ACCOUNTS,
    LIST_XERO_TAX_RATES,
    READ_XERO_BILL,
    SEARCH_XERO_CONTACTS,
    XERO_DEFINITIONS,
    build_xero_executors,
)


XERO_READ_PERMISSION = "xero.read"
XERO_BILL_WRITE_PERMISSION = "xero.bill.write"


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
    write_policy = AuthorityPolicy(
        frozenset({XERO_BILL_WRITE_PERMISSION}), approval_required=True
    )
    policies = {
        SEARCH_XERO_CONTACTS: read_policy,
        LIST_XERO_ACCOUNTS: read_policy,
        LIST_XERO_TAX_RATES: read_policy,
        FIND_XERO_BILL: read_policy,
        READ_XERO_BILL: read_policy,
        CREATE_XERO_DRAFT_BILL: write_policy,
        ATTACH_MAIL_DOCUMENT_TO_XERO_BILL: write_policy,
        AUTHORISE_XERO_BILL: write_policy,
    }
    return XeroRuntime(
        oauth,
        adapter,
        XERO_DEFINITIONS,
        policies,
        build_xero_executors(adapter, mail_account, call_id_source),
        frozenset({XERO_READ_PERMISSION, XERO_BILL_WRITE_PERMISSION}),
    )
