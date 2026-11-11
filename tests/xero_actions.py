"""Tests address the one `xero` capability by action (D-074)."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import ApprovalScope, CapabilityCall  # noqa: E402
from alx.tools.xero import XERO, XeroAction  # noqa: E402

SEARCH_CONTACTS = XeroAction.SEARCH_CONTACTS.value
CREATE_CONTACT = XeroAction.CREATE_CONTACT.value
UPDATE_CONTACT = XeroAction.UPDATE_CONTACT.value
LIST_ACCOUNTS = XeroAction.LIST_ACCOUNTS.value
LIST_TAX_RATES = XeroAction.LIST_TAX_RATES.value
FIND_BILL = XeroAction.FIND_BILL.value
READ_BILL = XeroAction.READ_BILL.value
DELETE_DRAFT_BILL = XeroAction.DELETE_DRAFT_BILL.value
CAPTURE_INVOICE = XeroAction.CAPTURE_INVOICE.value
CAPTURE_DIGIKEY_BILL = XeroAction.CAPTURE_DIGIKEY_BILL.value
IMPORT_DHL = XeroAction.IMPORT_DHL.value
FIND_QUOTES = XeroAction.FIND_QUOTES.value
INVOICE_QUOTE = XeroAction.INVOICE_QUOTE.value
LOOKUP_DIGIKEY = XeroAction.LOOKUP_DIGIKEY.value


def by_action(executors: Mapping[str, Callable]) -> dict[str, Callable[[Mapping[str, Any]], Any]]:
    """The one xero executor, addressed by action as each job once was by id."""
    return {
        item.value: (lambda arguments, value=item.value: executors[XERO]({"action": value, **arguments}))
        for item in XeroAction
    }


def xero_arguments(action: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"action": action, **(arguments or {})}


def xero_call(call_id: str, action: str, arguments: Mapping[str, Any] | None = None,
              approval_id: str | None = None) -> CapabilityCall:
    return CapabilityCall(call_id, XERO, xero_arguments(action, arguments), approval_id)


def xero_scope(action: str, arguments: Mapping[str, Any]) -> ApprovalScope:
    return ApprovalScope(XERO, xero_arguments(action, arguments))


def is_xero(call: Any, action: str) -> bool:
    """Whether a recorded call is this xero action."""
    return (call is not None and call.capability_id == XERO
            and call.arguments.get("action") == action)


def dhl_import(settings: Any, mail: Any = None, call_id_source: Callable[[], str] = lambda: "call") -> Any:
    """The DHL import action as live composition builds it from Xero settings."""
    from alx.bootstrap.dhl import build_dhl_import

    return build_dhl_import(
        mail if mail is not None else object(), call_id_source,
        settings.import_vat_account, settings.customs_duty_account,
        settings.clearance_account, settings.dhl_supplier_name,
        settings.unattended_bill_writes,
    )


def xero_executors(*args: Any, **kwargs: Any) -> dict[str, Callable[[Mapping[str, Any]], Any]]:
    """build_xero_executors, addressed by action."""
    from alx.tools.xero import build_xero_executors

    return by_action(build_xero_executors(*args, **kwargs))


def contract(action: str) -> Any:
    """An action's own input and result contract."""
    from alx.tools.xero import CONTRACTS

    return CONTRACTS[XeroAction(action)]


def gate_state(runtime: Any, action: str, permissions: Any = None, arguments: Mapping[str, Any] | None = None) -> Any:
    """What the one `xero` policy says about this action for these permissions."""
    from datetime import UTC, datetime

    from alx.safety import AuthorityContext, SafetyGate

    return SafetyGate(runtime.policies).evaluate(
        xero_call("probe", action, arguments or {}),
        AuthorityContext("friedl", runtime.permissions if permissions is None else frozenset(permissions),
                         datetime(2026, 8, 31, tzinfo=UTC)),
    ).state


def needs_approval(runtime: Any, action: str) -> bool:
    """Whether the one `xero` policy asks Friedl before this action (D-074)."""
    from alx.safety import SafetyState

    return gate_state(runtime, action) is SafetyState.APPROVAL_REQUIRED
