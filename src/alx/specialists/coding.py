"""Resolve accounting treatment from this organisation's own history.

Where a supplier's earlier bills all used the same account, that account is a
known answer and code may use it. Tax is not copied from those bills when the
invoice itself shows whether VAT was charged. Where there is no precedent, or
the precedent disagrees with itself, choosing the account is judgment and
returns to AL/X.

This never asks a model. Prior coding is a fact about the organisation, not an
opinion, and V1's habit of asking a model to pick an account every time is what
allowed a confident wrong answer.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def prior_coding(
    bills: Sequence[Mapping[str, Any]],
    default_account_code: str = "",
    invoice_shows_tax: bool | None = None,
    default_tax_type: str = "",
) -> dict[str, Any]:
    """Derive the settled coding for a supplier, or fall back to a default.

    `bills` are that supplier's existing accounts-payable bills, newest first.
    Discarded bills are excluded by the caller: a deleted bill is not evidence
    of how this supplier is treated.

    Where a supplier's bills all use one account, that account is used. Tax
    follows the invoice when the caller says whether it shows tax: the
    configured rate and Exclusive amounts when it does, no tax when it does
    not. Omitting that fact keeps the historical tax treatment. A taxed
    invoice with no configured rate stays unresolved rather than posting no
    tax or inventing a rate.

    Where its bills disagree, choosing between them is a policy decision no
    document contains: a supplier whose work spans consulting, travel and
    equipment has no single correct answer to derive. Rather than interrogate
    Friedl on every such invoice, or let a model guess an account and present
    the guess as knowledge, the configured default account is used and the
    tax type follows what the document itself shows.
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

    def fallback(reason: str) -> dict[str, Any]:
        if not default_account_code or invoice_shows_tax is None:
            return _unresolved(reason)
        tax_type = default_tax_type if invoice_shows_tax else "NONE"
        if not tax_type:
            return _unresolved(reason)
        return {
            "resolved": True,
            "account_code": default_account_code,
            "tax_type": tax_type,
            "line_amount_types": "Exclusive" if invoice_shows_tax else "NoTax",
            "based_on_bills": len(bills),
            "from_default": True,
            "reason": (
                f"{reason}; posted to the configured default account "
                f"{default_account_code} with tax {tax_type} taken from the "
                "document"
            ),
        }

    if not treatments:
        return fallback("no earlier bill for this supplier")

    distinct = set(treatments)
    if len(distinct) > 1:
        seen = sorted(f"{code}/{tax}" for code, tax, _ in distinct)
        return fallback(
            f"earlier bills disagree on treatment: {', '.join(seen)}"
        )

    code, tax_type, line_amount_types = treatments[0]
    # The account is the supplier's settled coding. The tax treatment is the
    # invoice's: earlier no-tax bills must not suppress VAT this document shows.
    if invoice_shows_tax is True:
        if not default_tax_type:
            return _unresolved(
                "the invoice shows tax but no tax rate is configured"
            )
        tax_type = default_tax_type
        line_amount_types = "Exclusive"
    elif invoice_shows_tax is False:
        tax_type = "NONE"
        line_amount_types = "NoTax"
    if invoice_shows_tax is None:
        reason = (
            f"every earlier bill for this supplier used account {code}"
            f" with tax type {tax_type}"
        )
    else:
        shown = "shows tax" if invoice_shows_tax else "shows no tax"
        reason = (
            f"every earlier bill for this supplier used account {code}; "
            f"this invoice {shown}, so the line uses tax type {tax_type}"
        )
    return {
        "resolved": True,
        "from_default": False,
        "account_code": code,
        "tax_type": tax_type,
        "line_amount_types": line_amount_types,
        "based_on_bills": len(bills),
        "reason": reason,
    }


def _unresolved(reason: str) -> dict[str, Any]:
    return {
        "resolved": False,
        "account_code": "",
        "tax_type": "",
        "line_amount_types": "",
        "based_on_bills": 0,
        "from_default": False,
        "reason": reason,
    }


def resolve_supplier(
    contacts: Sequence[Mapping[str, Any]], supplier_name: str
) -> dict[str, Any]:
    """Match a supplier only on its own name, never on what a search returned.

    Falling back from an exact match to whatever active contacts came back
    resolved "Expected Supplier" to "Completely Different Company" whenever the
    search returned one unrelated result. With unattended writes that posts a
    bill against the wrong company, so only the supplier's own name may
    identify it.
    """
    wanted = supplier_name.strip().casefold()
    if not wanted:
        return {"resolved": False, "contact_id": "", "reason": "no supplier name"}

    active = [
        item
        for item in contacts
        if str(item.get("ContactStatus") or "ACTIVE") == "ACTIVE"
    ]
    candidates = [
        item for item in active if str(item.get("Name") or "").strip().casefold() == wanted
    ]
    if not candidates:
        near = sorted(str(item.get("Name") or "") for item in active)
        return {
            "resolved": False,
            "contact_id": "",
            "reason": (
                f"no contact is named {supplier_name!r}"
                + (f"; the search returned {', '.join(near)}" if near else "")
            ),
        }
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
