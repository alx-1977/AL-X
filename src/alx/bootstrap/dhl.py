"""Compose the one DHL import path into AL/X's boundaries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Mapping

from alx.contracts import (
    CapabilityDefinition,
    CapabilityResult,
    MailAccount,
    StructuredData,
    XeroAccountingAccount,
)
from alx.providers import DhlImportAnalyzerAdapter
from alx.safety import AuthorityPolicy
from alx.tools import (
    DHL_DEFINITIONS,
    PROCESS_DHL_IMPORT,
    build_dhl_executors,
)


DHL_IMPORT_PERMISSION = "dhl.import.process"


@dataclass(frozen=True, slots=True)
class DhlRuntime:
    definitions: tuple[CapabilityDefinition, ...]
    policies: Mapping[str, AuthorityPolicy]
    executors: Mapping[str, Callable[[StructuredData], CapabilityResult]]
    permissions: frozenset[str]


def build_dhl_runtime(
    mail: MailAccount,
    account: XeroAccountingAccount,
    call_id_source: Callable[[], str],
    import_vat_account: str,
    customs_duty_account: str,
    clearance_account: str,
    supplier_name: str = "",
    unattended: bool = False,
) -> DhlRuntime:
    """Build the single DHL import capability.

    Processing a DHL import writes to Xero, so it carries the same authority as
    any other bill write: unattended where Friedl configured that under D-018,
    and asking otherwise.
    """
    return DhlRuntime(
        DHL_DEFINITIONS,
        {
            PROCESS_DHL_IMPORT: AuthorityPolicy(
                frozenset({DHL_IMPORT_PERMISSION}),
                approval_required=not unattended,
            )
        },
        build_dhl_executors(
            mail,
            DhlImportAnalyzerAdapter(),
            account,
            call_id_source,
            import_vat_account,
            customs_duty_account,
            clearance_account,
            supplier_name,
        ),
        frozenset({DHL_IMPORT_PERMISSION}),
    )
