"""Compose the one DHL import path; it is offered as an action of `xero` (D-074)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from alx.contracts import (
    CapabilityResult,
    MailAccount,
    StructuredData,
    XeroAccountingAccount,
    XeroAction,
)
from alx.providers import DhlImportAnalyzerAdapter
from alx.tools import build_dhl_executors


DHL_IMPORT_PERMISSION = "dhl.import.process"


@dataclass(frozen=True, slots=True)
class DhlImport:
    """The DHL import's executor, bound to an account later, and its own authority."""

    executor_for: Callable[[XeroAccountingAccount], Callable[[StructuredData], CapabilityResult]]
    permission: str
    approval_required: bool


def build_dhl_import(
    mail: MailAccount,
    call_id_source: Callable[[], str],
    import_vat_account: str,
    customs_duty_account: str,
    clearance_account: str,
    supplier_name: str = "",
    unattended: bool = False,
    processed_mailbox: str = "",
) -> DhlImport:
    """Build the single DHL import action.

    Processing a DHL import writes to Xero, so it carries the same authority as
    any other bill write: unattended where Friedl configured that under D-018,
    and asking otherwise. The `xero` runtime binds it to its own adapter.
    """

    def executor_for(account: XeroAccountingAccount):
        return build_dhl_executors(
            mail,
            DhlImportAnalyzerAdapter(),
            account,
            call_id_source,
            import_vat_account,
            customs_duty_account,
            clearance_account,
            supplier_name,
            processed_mailbox=processed_mailbox,
        )[XeroAction.IMPORT_DHL]

    return DhlImport(executor_for, DHL_IMPORT_PERMISSION, not unattended)
