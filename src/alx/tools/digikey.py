"""Structured order reads and mechanical conversion to invoice values."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from alx.contracts import (
    CapabilityDefinition, CapabilityResult, CapabilityResultState, ContentOrigin,
    RetentionPolicy, SideEffect, StructuredSchema, ValueKind,
)


LOOKUP_DIGIKEY_ORDER = "lookup_digikey_order"
CAPTURE_DIGIKEY_ORDER_BILL = "capture_digikey_order_bill"
FAILURE_CODES = (
    "arguments_unusable", "not_configured", "authentication_failed", "rate_limited",
    "provider_failed", "connection_failed", "response_invalid", "order_not_found",
    "order_ambiguous", "search_incomplete",
)
_STRING = StructuredSchema(ValueKind.STRING)


def _object(properties: Mapping[str, StructuredSchema]) -> StructuredSchema:
    return StructuredSchema(ValueKind.OBJECT, properties, tuple(properties), extra_properties=False)


_SHIPMENT = _object({key: _STRING for key in (
    "invoice_number", "ship_date", "invoice_date", "quantity", "carrier", "tracking_number", "eta"
)})
_LINE = _object({
    **{key: _STRING for key in (
        "digikey_part_number", "manufacturer_part_number", "description", "quantity",
        "unit_price", "extended_price", "country_of_origin", "quantity_shipped",
    )},
    "shipments": StructuredSchema(ValueKind.ARRAY, items=_SHIPMENT),
})
ORDER_SCHEMA = _object({
    **{key: _STRING for key in (
        "sales_order_number", "status", "invoice_date", "currency", "subtotal", "shipping", "tax", "total"
    )},
    "invoice_numbers": StructuredSchema(ValueKind.ARRAY, items=_STRING),
    "shipments": StructuredSchema(ValueKind.ARRAY, items=_SHIPMENT),
    "line_items": StructuredSchema(ValueKind.ARRAY, items=_LINE),
})
ORDER_INPUT = StructuredSchema(
    ValueKind.OBJECT, {"sales_order_number": _STRING, "invoice_number": _STRING},
    extra_properties=False,
)
LOOKUP_DEFINITION = CapabilityDefinition(
    LOOKUP_DIGIKEY_ORDER,
    "Read a DigiKey order by exactly one sales_order_number or invoice_number. Returns all lines, invoices and shipping facts; empty values are unknown. Invoice search scans paginated account history and refuses ambiguous matches.",
    ORDER_INPUT, ORDER_SCHEMA, SideEffect.NONE, FAILURE_CODES,
)


def order_arguments(arguments: Mapping[str, Any]) -> dict[str, str]:
    supplied = {key: arguments[key] for key in ("sales_order_number", "invoice_number") if key in arguments}
    if len(supplied) != 1:
        raise ValueError("order_identifier")
    for key, value in supplied.items():
        if (not isinstance(value, str) or not value.isascii() or not value.isdigit()
                or len(value) > 10 or int(value) <= 0):
            raise ValueError(key)
    return supplied


def build_digikey_executors(provider: Any, call_id_source: Callable[[], str]):
    def lookup(arguments: Mapping[str, Any]) -> CapabilityResult:
        try:
            selected = order_arguments(arguments)
        except ValueError:
            return CapabilityResult(call_id_source(), LOOKUP_DIGIKEY_ORDER,
                                    CapabilityResultState.FAILED, failure={"code": "arguments_unusable"})
        try:
            values = provider.lookup(**selected)
        except Exception as error:
            code = getattr(error, "code", "provider_failed")
            return CapabilityResult(call_id_source(), LOOKUP_DIGIKEY_ORDER,
                                    CapabilityResultState.FAILED,
                                    failure={"code": code if code in FAILURE_CODES else "provider_failed"})
        return CapabilityResult(
            call_id_source(), LOOKUP_DIGIKEY_ORDER, CapabilityResultState.SUCCEEDED, values,
            provenance=RetentionPolicy().non_mail(ContentOrigin.EXTERNAL, datetime.now(UTC)),
        )
    return {LOOKUP_DIGIKEY_ORDER: lookup}


def bill_invoice(order: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]], list[str]]:
    """Require one fully shipped invoice and an explicit reconciled breakdown.

    No policy-based allocation of shipping, partial quantities, tax or multiple
    invoices is permitted. Missing amounts are not evidence of zero charges.
    """
    invoice = {"supplier_name": "DigiKey", "document_type": "supplier_invoice",
               "invoice_number": "", "invoice_date": "", "due_date": "",
               "currency": order["currency"], "subtotal": "", "tax_amount": order["tax"],
               "total": order["total"], "description": "DigiKey " + order["sales_order_number"],
               "order": dict(order)}
    lines: list[dict[str, str]] = []
    problems: list[str] = []
    if len(order["invoice_numbers"]) != 1:
        problems.append("invoice_allocation_unresolved")
    else:
        invoice["invoice_number"] = order["invoice_numbers"][0]
    dates = {item["invoice_date"][:10] or item["ship_date"][:10] for item in order["shipments"]}
    if order["invoice_date"]:
        dates = {order["invoice_date"][:10]}
    try:
        if len(dates) != 1:
            raise ValueError("invoice_date")
        invoice["invoice_date"] = date.fromisoformat(next(iter(dates))).isoformat()
    except ValueError:
        problems.append("invoice_date_unresolved")
    currency = order["currency"]
    if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
        problems.append("currency_unresolved")
    try:
        def amount(value: str) -> Decimal:
            result = Decimal(value)
            if not result.is_finite() or result < 0:
                raise ValueError("amount")
            return result
        shipping, tax, total = (amount(order[key]) for key in ("shipping", "tax", "total"))
        merchandise = Decimal(0)
        rounded_lines = shipping.quantize(Decimal("0.01"))
        if not order["line_items"] or total <= 0:
            raise ValueError("line_items")
        for line in order["line_items"]:
            quantity, unit, extension = (amount(line[key]) for key in ("quantity", "unit_price", "extended_price"))
            if quantity <= 0 or quantity != amount(line["quantity_shipped"]):
                raise ValueError("incomplete_shipment")
            shipments = line["shipments"]
            if (not shipments or any(item["invoice_number"] != invoice["invoice_number"] for item in shipments)
                    or sum((amount(item["quantity"]) for item in shipments), Decimal(0)) != quantity):
                raise ValueError("invoice_allocation")
            if abs(quantity * unit - extension) > Decimal("0.01"):
                raise ValueError("line_extension")
            merchandise += extension
            rounded_lines += (quantity * unit).quantize(Decimal("0.01"))
            lines.append({"description": " | ".join(filter(None, (
                line["digikey_part_number"], line["manufacturer_part_number"], line["description"]
            ))), "quantity": str(quantity), "unit_amount": str(unit)})
        if (abs(merchandise - amount(order["subtotal"])) > Decimal("0.01")
                or abs(merchandise + shipping + tax - total) > Decimal("0.01")):
            problems.append("totals_mismatch")
        elif (merchandise + shipping + tax).quantize(Decimal("0.01")) != total.quantize(Decimal("0.01")) or rounded_lines + tax != total:
            # The existing writer verifies exact cent totals. A cent-level
            # difference needs a chosen rounding allocation before posting,
            # even when it is inside the reconciliation tolerance.
            problems.append("rounding_allocation_unresolved")
        if tax:
            problems.append("tax_allocation_unresolved")
        invoice["subtotal"] = str(merchandise + shipping)
        lines.append({"description": "Shipping", "quantity": "1", "unit_amount": str(shipping)})
    except (InvalidOperation, ValueError):
        problems.append("billing_values_unresolved")
    return invoice, lines, problems
