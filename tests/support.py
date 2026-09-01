"""Shared construction helpers for tests.

Settings are built by keyword here so that adding a configuration field
cannot silently shift a positional argument in an unrelated test.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.config import XeroSettings  # noqa: E402


def xero_settings(
    *,
    unattended_bill_writes: bool = False,
    unattended_bill_deletes: bool = False,
    approval_ttl_seconds: int = 300,
    default_account_code: str = "",
    default_tax_type: str = "",
    import_vat_account: str = "820",
    customs_duty_account: str = "426",
    clearance_account: str = "425",
    dhl_supplier_name: str = "DHL EXPRESS",
) -> XeroSettings:
    return XeroSettings(
        client_id="id",
        client_secret="secret",
        redirect_uri="http://localhost/callback",
        tenant_id="",
        timeout_seconds=10,
        approval_ttl_seconds=approval_ttl_seconds,
        unattended_bill_writes=unattended_bill_writes,
        unattended_bill_deletes=unattended_bill_deletes,
        default_account_code=default_account_code,
        default_tax_type=default_tax_type,
        import_vat_account=import_vat_account,
        customs_duty_account=customs_duty_account,
        clearance_account=clearance_account,
        dhl_supplier_name=dhl_supplier_name,
    )
