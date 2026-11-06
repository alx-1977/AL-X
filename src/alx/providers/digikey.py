"""Production Order Status v4 HTTP boundary. No rotating refresh tokens."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx


class DigiKeyError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _object(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DigiKeyError("response_invalid")
    return value


def _rows(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise DigiKeyError("response_invalid")
    return value


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, Decimal)) and not isinstance(value, bool):
        return str(value)
    raise DigiKeyError("response_invalid")


def _amount(value: Any) -> str:
    if value is None:
        return ""
    try:
        amount = Decimal(str(value))
        if amount.is_finite():
            return format(amount, "f")
    except InvalidOperation:
        pass
    raise DigiKeyError("response_invalid")


def normalize_order(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Whitelist protocol fields; absent shipping/tax charges are zero.

    v4's published schema has TotalPrice but no guaranteed shipping/tax
    breakdown. Preserve explicit breakdowns when returned; never classify the
    difference as shipping or tax.
    """
    shipments: list[dict[str, str]] = []
    lines = []
    invoices: list[str] = []
    address = _object(raw.get("ShippingAddress") or {})
    address_invoice = _text(address.get("InvoiceId"))
    if address_invoice and address_invoice != "0":
        invoices.append(address_invoice)
    for value in _rows(raw.get("LineItems")):
        line = _object(value)
        item_shipments = []
        raw_shipments = line.get("ItemShipments")
        for value in _rows([] if raw_shipments is None else raw_shipments):
            shipment = _object(value)
            number = _text(shipment.get("InvoiceId"))
            if number and number != "0" and number not in invoices:
                invoices.append(number)
            entry = {
                "invoice_number": number,
                "ship_date": _text(shipment.get("ShippedDate")),
                "invoice_date": _text(shipment.get("InvoiceDate")),
                "quantity": _amount(shipment.get("QuantityShipped")),
                "carrier": _text(raw.get("ShipMethod")),
                "tracking_number": _text(shipment.get("TrackingNumber")),
                "eta": _text(shipment.get("ExpectedDeliveryDate")),
            }
            item_shipments.append(entry)
            if entry not in shipments:
                shipments.append(entry)
        quantity, unit = _amount(line.get("QuantityOrdered")), _amount(line.get("UnitPrice"))
        # TotalPrice on a line is documented as including charges/taxes. The
        # merchandise extension is the price times the accepted quantity.
        extension = format(Decimal(quantity) * Decimal(unit), "f") if quantity and unit else ""
        lines.append({
            "digikey_part_number": _text(line.get("DigiKeyProductNumber")),
            "manufacturer_part_number": _text(line.get("ManufacturerProductNumber")),
            "description": _text(line.get("Description")),
            "quantity": quantity, "unit_price": unit, "extended_price": extension,
            "country_of_origin": _text(line.get("CountryOfOrigin")),
            "quantity_shipped": _amount(line.get("QuantityShipped")),
            "shipments": item_shipments,
        })
    subtotal = (format(sum((Decimal(line["extended_price"]) for line in lines), Decimal(0)), "f")
                if lines and all(line["extended_price"] for line in lines) else "")
    return {
        "sales_order_number": _text(raw.get("SalesOrderId")),
        "status": _text(_object(raw.get("Status") or {}).get("SalesOrderStatus")),
        "invoice_numbers": invoices, "shipments": shipments,
        "invoice_date": _text(raw.get("InvoiceDate")),
        "currency": _text(raw.get("Currency")),
        "subtotal": _amount(raw["Subtotal"]) if "Subtotal" in raw else subtotal,
        "shipping": _amount(0 if raw.get("ShippingCharges") in (None, "") else raw["ShippingCharges"]),
        "tax": _amount(0 if raw.get("SalesTax") in (None, "") else raw["SalesTax"]),
        "total": _amount(raw.get("TotalPrice")), "line_items": lines,
    }


class DigiKeyProvider:
    def __init__(self, client_id: str, client_secret: str, account_id: str,
                 *, client: Any = None) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._account_id = account_id
        self._client = client or httpx

    def _request(self, method: str, url: str, **kwargs: Any) -> Mapping[str, Any]:
        code = ""
        try:
            response = self._client.request(method, url, timeout=60, **kwargs)
        except httpx.RequestError:
            code = "connection_failed"
        # Raise outside the HTTP exception handler so request-bearing errors
        # cannot expose credentials through exception chains or diagnostics.
        if code:
            raise DigiKeyError(code)
        status = response.status_code
        if status >= 400:
            raise DigiKeyError({401: "authentication_failed", 403: "authentication_failed",
                                404: "order_not_found", 429: "rate_limited"}.get(status, "provider_failed"))
        try:
            body = json.loads(response.text, parse_float=Decimal)
        except (ValueError, TypeError):
            body = None
        return _object(body)

    def lookup(self, *, sales_order_number: str = "", invoice_number: str = "") -> dict[str, Any]:
        if not (self._client_id and self._client_secret and self._account_id):
            raise DigiKeyError("not_configured")
        token = self._request("POST", "https://api.digikey.com/v1/oauth2/token", data={
            "client_id": self._client_id, "client_secret": self._client_secret,
            "grant_type": "client_credentials",
        }).get("access_token")
        if not isinstance(token, str) or not token:
            raise DigiKeyError("authentication_failed")
        headers = {"Authorization": f"Bearer {token}", "X-DIGIKEY-Client-Id": self._client_id,
                   "X-DIGIKEY-Account-Id": self._account_id}
        base = "https://api.digikey.com/orderstatus/v4"
        if invoice_number:
            matches: set[str] = set()
            seen = 0
            checked: set[str] = set()
            # SearchOrders has no invoice filter. Search all account orders,
            # Resolve history candidates through sales-order detail: history
            # can omit shipment invoice facts. Finish all pages for ambiguity.
            for page in range(1, 1001):
                body = self._request("GET", base + "/orders", headers=headers, params={
                    "Shared": "true", "StartDate": "1900-01-01", "EndDate": date.today().isoformat(),
                    "PageNumber": page, "PageSize": 25,
                })
                orders = _rows(body.get("Orders"))
                count = body.get("TotalOrders")
                if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                    raise DigiKeyError("response_invalid")
                for order in orders:
                    for sale in _rows(_object(order).get("SalesOrders")):
                        number = _text(_object(sale).get("SalesOrderId"))
                        if not number.isascii() or not number.isdigit() or int(number) <= 0:
                            raise DigiKeyError("response_invalid")
                        if number in checked:
                            continue
                        checked.add(number)
                        normalized = normalize_order(self._request(
                            "GET", base + "/salesorder/" + number, headers=headers))
                        if normalized["sales_order_number"] != number:
                            raise DigiKeyError("response_invalid")
                        if invoice_number in normalized["invoice_numbers"]:
                            matches.add(normalized["sales_order_number"])
                seen += len(orders)
                if seen >= count:
                    break
                if not orders:
                    raise DigiKeyError("response_invalid")
            else:
                raise DigiKeyError("search_incomplete")
            if len(matches) != 1 or "" in matches:
                raise DigiKeyError("order_ambiguous" if matches else "order_not_found")
            sales_order_number = matches.pop()
        order = normalize_order(self._request("GET", base + "/salesorder/" + sales_order_number, headers=headers))
        if order["sales_order_number"] != sales_order_number or (
            invoice_number and invoice_number not in order["invoice_numbers"]
        ):
            raise DigiKeyError("response_invalid")
        return order
