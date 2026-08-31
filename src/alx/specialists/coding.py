"""Resolve accounting treatment from this organisation's own history.

Where a supplier's earlier bills were all coded the same way, that is a known
answer and code may use it. Where there is no precedent, or the precedent
disagrees with itself, choosing the treatment is judgment and returns to AL/X.

This never asks a model. Prior coding is a fact about the organisation, not an
opinion, and V1's habit of asking a model to pick an account every time is what
allowed a confident wrong answer.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def prior_coding(bills: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Derive the settled coding for a supplier, or say why there is none.

    `bills` are that supplier's existing accounts-payable bills, newest first.
    Discarded bills are excluded by the caller: a deleted bill is not evidence
    of how this supplier is treated.
    """
    treatments: list[tuple[str, str, str]] = []
    for bill in bills:
        line_amount_types = str(bill.get("LineAmountTypes") or "")
        for line in bill.get("LineItems") or ():
            if not isinstance(line, Mapping):
                continue
            code = str(line.get("AccountCode") or "")
            tax_type = str(line.get("TaxType") or "")
            if code:
                treatments.append((code, tax_type, line_amount_types))

    if not treatments:
        return _unresolved("no earlier bill for this supplier")

    distinct = set(treatments)
    if len(distinct) > 1:
        seen = sorted(f"{code}/{tax}" for code, tax, _ in distinct)
        return _unresolved(
            f"earlier bills disagree on treatment: {', '.join(seen)}"
        )

    code, tax_type, line_amount_types = treatments[0]
    return {
        "resolved": True,
        "account_code": code,
        "tax_type": tax_type,
        "line_amount_types": line_amount_types,
        "based_on_bills": len(bills),
        "reason": (
            f"every earlier bill for this supplier used account {code}"
            f" with tax type {tax_type}"
        ),
    }


def _unresolved(reason: str) -> dict[str, Any]:
    return {
        "resolved": False,
        "account_code": "",
        "tax_type": "",
        "line_amount_types": "",
        "based_on_bills": 0,
        "reason": reason,
    }


def resolve_supplier(
    contacts: Sequence[Mapping[str, Any]], supplier_name: str
) -> dict[str, Any]:
    """Match a supplier only where exactly one candidate is unambiguous."""
    wanted = supplier_name.strip().casefold()
    if not wanted:
        return {"resolved": False, "contact_id": "", "reason": "no supplier name"}

    active = [
        item
        for item in contacts
        if str(item.get("ContactStatus") or "ACTIVE") == "ACTIVE"
    ]
    exact = [
        item for item in active if str(item.get("Name") or "").strip().casefold() == wanted
    ]
    candidates = exact or active
    if not candidates:
        return {"resolved": False, "contact_id": "", "reason": "no matching contact"}
    if len(candidates) > 1:
        names = sorted(str(item.get("Name") or "") for item in candidates)
        return {
            "resolved": False,
            "contact_id": "",
            "reason": f"several contacts match: {', '.join(names)}",
        }
    contact = candidates[0]
    return {
        "resolved": True,
        "contact_id": str(contact.get("ContactID") or ""),
        "contact_name": str(contact.get("Name") or ""),
        "reason": "one unambiguous contact",
    }
