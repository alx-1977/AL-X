"""HTTP-mocked v4 orders and the single structured supplier-bill path."""

import ast
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alx.bootstrap.digikey import DIGIKEY_ORDER_READ_PERMISSION, build_digikey_provider
from alx.bootstrap.xero import build_xero_runtime, BILL_EXECUTION_ACTIONS
from alx.config.digikey import DigiKeySettings
from alx.contracts import CapabilityResultState, ExecutionOutcome, SideEffect
from alx.providers.digikey import DigiKeyError, DigiKeyProvider, normalize_order
from alx.tools.digikey import (  # noqa: E402
    LOOKUP_DEFINITION,
    build_digikey_executors,
)
from alx.tools.xero import (  # noqa: E402
    CAPTURE_DIGIKEY_DEFINITION,
)
from test_invoice_capture import FakeXero, FakeMail, arguments, extracted
from xero_actions import (  # noqa: E402
    CAPTURE_DIGIKEY_BILL,
    LOOKUP_DIGIKEY,
    XERO,
    by_action,
    xero_arguments,
    xero_call,
    xero_executors,
    needs_approval,
)
from alx.contracts import XeroAction  # noqa: E402
from support import xero_settings


ENV = {"DIGIKEY_CLIENT_ID": "client-private", "DIGIKEY_CLIENT_SECRET": "secret-private",
       "DIGIKEY_ACCOUNT_ID": "15219690", "DIGIKEY_REFRESH_TOKEN": "refresh-private"}


def raw_order():
    return json.loads((ROOT / "tests/fixtures/digikey/salesorder_v4.json").read_text())


def complete_order():
    # v4 guarantees only TotalPrice. Explicit charge fields, when returned,
    # allow capture; a schema-only response must return missing evidence.
    return {**raw_order(), "ShippingCharges": 460, "SalesTax": 0}


def provider(handler):
    return DigiKeyProvider(ENV["DIGIKEY_CLIENT_ID"], ENV["DIGIKEY_CLIENT_SECRET"],
                          ENV["DIGIKEY_ACCOUNT_ID"], client=httpx.Client(transport=httpx.MockTransport(handler)))


def handler_with_order(order=None):
    calls = []
    def handle(request):
        calls.append(request)
        if request.url.path == "/v1/oauth2/token":
            assert request.method == "POST"
            assert b"grant_type=client_credentials" in request.content
            assert b"refresh" not in request.content
            return httpx.Response(200, json={"access_token": "token-private"})
        assert request.headers["X-DIGIKEY-Account-Id"] == "15219690"
        assert request.headers["X-DIGIKEY-Client-Id"] == ENV["DIGIKEY_CLIENT_ID"]
        assert request.headers["Authorization"] == "Bearer token-private"
        assert request.url.host == "api.digikey.com"
        return httpx.Response(200, json=order or complete_order())
    return handle, calls


def test_sales_order_lookup_and_contract(caplog):
    handler, calls = handler_with_order()
    execute = build_digikey_executors(provider(handler).lookup, lambda: "call")[LOOKUP_DIGIKEY]
    result = execute({"sales_order_number": "102748548"})
    assert result.state is CapabilityResultState.SUCCEEDED
    assert LOOKUP_DEFINITION.output_schema.accepts(result.values)
    assert LOOKUP_DEFINITION.side_effect is SideEffect.NONE
    assert result.values["invoice_numbers"] == ("135916797",)
    assert result.values["total"] == "2464.91"
    assert result.values["currency"] == "ZAR"
    assert result.values["status"] == "Shipped"
    assert result.values["subtotal"] == "2004.910"
    assert result.values["shipping"] == "460"
    assert result.values["tax"] == "0"
    line = result.values["line_items"][0]
    assert line["digikey_part_number"] == "TEST-1-ND"
    assert line["manufacturer_part_number"] == "TEST-1"
    assert line["country_of_origin"] == "MY"
    assert line["unit_price"] == "200.491"
    assert line["quantity"] == "10"
    assert line["extended_price"] == "2004.910"
    shipment = result.values["shipments"][0]
    assert shipment["carrier"] == "DHL"
    assert shipment["tracking_number"] == "1697298993"
    assert shipment["eta"] == "2026-11-06"
    assert calls[-1].url.path == "/orderstatus/v4/salesorder/102748548"
    for secret in (*ENV.values(), "token-private"):
        if secret != "15219690":
            assert secret not in repr(result) + caplog.text


def test_schema_only_response_defaults_absent_charges_to_zero():
    result = normalize_order(raw_order())
    assert result["shipping"] == result["tax"] == "0"


def test_invoice_search_paginates_and_retrieves_exact_order():
    base, calls = handler_with_order()
    sales = {}
    def handle(request):
        if request.url.path.endswith("/orders"):
            calls.append(request)
            page = int(request.url.params["PageNumber"])
            assert request.url.params["Shared"] == "true"
            assert request.headers["X-DIGIKEY-Account-Id"] == "15219690"
            sale = complete_order()
            if page == 1:
                sale["SalesOrderId"] = 111
                sale["LineItems"][0]["ItemShipments"][0]["InvoiceId"] = 999
            sales[str(sale["SalesOrderId"])] = sale
            return httpx.Response(200, json={"TotalOrders": 2, "Orders": [{"SalesOrders": [{"SalesOrderId": sale["SalesOrderId"]}]}]})
        if "/salesorder/" in request.url.path:
            calls.append(request)
            return httpx.Response(200, json=sales[request.url.path.rsplit("/", 1)[1]])
        return base(request)
    result = provider(handle).lookup(invoice_number="135916797")
    assert result["sales_order_number"] == "102748548"
    assert result["invoice_numbers"] == ["135916797"]
    assert len(calls) == 5


def invoice_history_handler(numbers, *, total=None, match=False, matching_numbers=()):
    base, calls = handler_with_order()
    def handle(request):
        if request.url.path.endswith("/orders"):
            calls.append(request)
            return httpx.Response(200, json={
                "TotalOrders": len(numbers) if total is None else total,
                "Orders": [{"SalesOrders": [{"SalesOrderId": n}]} for n in numbers],
            })
        if "/salesorder/" in request.url.path:
            calls.append(request)
            raw = complete_order()
            raw["SalesOrderId"] = int(request.url.path.rsplit("/", 1)[1])
            if not match and raw["SalesOrderId"] not in matching_numbers:
                raw["LineItems"][0]["ItemShipments"][0]["InvoiceId"] = 999
            return httpx.Response(200, json=raw)
        return base(request)
    return handle, calls


@pytest.mark.parametrize("capture_bill", [False, True])
@pytest.mark.parametrize("paginated", [False, True])
def test_invoice_search_rejects_distinct_matching_orders(capture_bill, paginated):
    base, calls = invoice_history_handler([111, 222], match=True)
    def handle(request):
        if paginated and request.url.path.endswith("/orders"):
            calls.append(request)
            number = 111 if request.url.params["PageNumber"] == "1" else 222
            return httpx.Response(200, json={"TotalOrders": 2, "Orders": [
                {"SalesOrders": [{"SalesOrderId": number}]}]})
        return base(request)
    p = provider(handle)
    xero = FakeXero()
    xero.contacts = ({"Name": "DigiKey", "ContactID": "c-1", "ContactStatus": "ACTIVE"},)
    xero.create_draft_bill = Mock(wraps=xero.create_draft_bill)
    xero.authorise_bill = Mock(wraps=xero.authorise_bill)
    if capture_bill:
        execute = xero_executors(xero, FakeMail(), lambda: "call",
                                       digikey_lookup=p.lookup)[CAPTURE_DIGIKEY_BILL]
    else:
        execute = build_digikey_executors(p.lookup, lambda: "call")[LOOKUP_DIGIKEY]
    result = execute({"invoice_number": "135916797"})
    assert result.state is CapabilityResultState.FAILED
    assert result.failure == {"code": "order_ambiguous"}
    assert [r.url.path for r in calls if "/salesorder/" in r.url.path] == [
        "/orderstatus/v4/salesorder/111", "/orderstatus/v4/salesorder/222"]
    assert xero.created == 0
    xero.create_draft_bill.assert_not_called()
    xero.authorise_bill.assert_not_called()


def test_invoice_search_returns_unique_match_only_after_all_pages(monkeypatch):
    import alx.providers.digikey as module

    monkeypatch.setattr(module, "INVOICE_SEARCH_MAX_REQUESTS", 5)
    base, calls = invoice_history_handler([], matching_numbers=(111,))
    def handle(request):
        if request.url.path.endswith("/orders"):
            calls.append(request)
            numbers = [111] if request.url.params["PageNumber"] == "1" else [111, 222]
            return httpx.Response(200, json={"TotalOrders": 3, "Orders": [
                {"SalesOrders": [{"SalesOrderId": n}]} for n in numbers]})
        return base(request)
    result = provider(handle).lookup(invoice_number="135916797")
    assert result["sales_order_number"] == "111"
    # Repeated references to 111 neither imply ambiguity nor repeat detail reads.
    assert [r.url.path for r in calls] == [
        "/v1/oauth2/token", "/orderstatus/v4/orders", "/orderstatus/v4/salesorder/111",
        "/orderstatus/v4/orders", "/orderstatus/v4/salesorder/222"]


@pytest.mark.parametrize("exhaustion", ["requests", "deadline", "timeout"])
@pytest.mark.parametrize("capture_bill", [False, True])
@pytest.mark.parametrize("after_match", [False, True])
def test_incomplete_invoice_search_never_writes(monkeypatch, exhaustion, capture_bill, after_match):
    import alx.providers.digikey as module

    monkeypatch.setattr(module, "INVOICE_SEARCH_MAX_REQUESTS", 4, raising=False)
    monkeypatch.setattr(module, "INVOICE_SEARCH_TIMEOUT", 10, raising=False)
    clock = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0], raising=False)
    base, calls = invoice_history_handler([111, 222, 333],
                                         matching_numbers=(111,) if after_match else ())
    def handle(request):
        response = base(request)
        if exhaustion == "deadline":
            clock[0] += 3 if after_match else 4
        if exhaustion == "timeout" and "/salesorder/" in request.url.path and (
            not after_match or request.url.path.endswith("/222")
        ):
            raise httpx.ReadTimeout("private", request=request)
        return response
    p = provider(handle)
    if capture_bill:
        xero = FakeXero()
        xero.contacts = ({"Name": "DigiKey", "ContactID": "c-1", "ContactStatus": "ACTIVE"},)
        xero.create_draft_bill = Mock(wraps=xero.create_draft_bill)
        xero.authorise_bill = Mock(wraps=xero.authorise_bill)
        execute = xero_executors(xero, FakeMail(), lambda: "call",
                                       digikey_lookup=p.lookup)[CAPTURE_DIGIKEY_BILL]
    else:
        execute = build_digikey_executors(p.lookup, lambda: "call")[LOOKUP_DIGIKEY]
    result = execute({"invoice_number": "135916797"})
    assert result.state is CapabilityResultState.FAILED
    assert result.failure == {"code": "search_incomplete"}
    assert len(calls) == (4 if after_match else {"requests": 4, "deadline": 3, "timeout": 3}[exhaustion])
    if exhaustion == "deadline":
        assert [r.extensions["timeout"]["read"] for r in calls] == ([10, 7, 4, 1] if after_match else [10, 6, 2])
    if capture_bill:
        assert xero.created == 0
        xero.create_draft_bill.assert_not_called()
        xero.authorise_bill.assert_not_called()


def test_completed_invoice_search_not_found(monkeypatch):
    import alx.providers.digikey as module

    monkeypatch.setattr(module, "INVOICE_SEARCH_MAX_REQUESTS", 3, raising=False)
    handler, calls = invoice_history_handler([111])
    with pytest.raises(DigiKeyError, match="^order_not_found$"):
        provider(handler).lookup(invoice_number="135916797")
    assert len(calls) == 3


@pytest.mark.parametrize("limit", [1, 3])
def test_invoice_request_budget_includes_authentication_and_pagination(monkeypatch, limit):
    import alx.providers.digikey as module

    monkeypatch.setattr(module, "INVOICE_SEARCH_MAX_REQUESTS", limit)
    # Repeated candidates must not trigger repeated detail requests, but each
    # history page still consumes budget even when it adds no new candidates.
    handler, calls = invoice_history_handler([111], total=100)
    with pytest.raises(DigiKeyError, match="^search_incomplete$"):
        provider(handler).lookup(invoice_number="135916797")
    assert len(calls) == limit
    assert sum("/salesorder/" in r.url.path for r in calls) == (limit == 3)


@pytest.mark.parametrize("late_phase", ["token", "match"])
def test_invoice_search_rejects_results_received_after_deadline(monkeypatch, late_phase):
    import alx.providers.digikey as module

    clock = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    handler, calls = invoice_history_handler([111], match=True)
    def handle(request):
        response = handler(request)
        if (late_phase == "token" or "/salesorder/" in request.url.path):
            clock[0] = module.INVOICE_SEARCH_TIMEOUT
        return response
    with pytest.raises(DigiKeyError, match="^search_incomplete$"):
        provider(handle).lookup(invoice_number="135916797")
    assert len(calls) == (1 if late_phase == "token" else 3)


@pytest.mark.parametrize("status,code", [(401, "authentication_failed"), (400, "provider_failed"),
                                          (429, "rate_limited"), (404, "order_not_found")])
def test_failures_do_not_disclose_http_body_or_secrets(status, code, caplog):
    p = provider(lambda request: httpx.Response(status, text="secret-private token-private refresh-private"))
    result = build_digikey_executors(p.lookup, lambda: "call")[LOOKUP_DIGIKEY]({"sales_order_number": "1"})
    assert result.failure == {"code": code}
    assert "private" not in repr(result) + caplog.text


def test_connection_error_and_invalid_json_are_sanitized():
    def handle(request):
        raise httpx.ConnectError("secret-private", request=request)
    p = provider(handle)
    with pytest.raises(DigiKeyError) as caught:
        p.lookup(sales_order_number="1")
    assert caught.value.code == "connection_failed"
    assert caught.value.__context__ is None
    p = provider(lambda _: httpx.Response(200, text="secret-private"))
    with pytest.raises(DigiKeyError, match="response_invalid"):
        p.lookup(sales_order_number="1")


@pytest.mark.parametrize("args", [{}, {"invoice_number": "1", "sales_order_number": "2"},
                                  {"sales_order_number": "../token"}, {"invoice_number": ""}])
def test_bad_identifiers_do_not_make_http_requests(args):
    p = Mock()
    result = build_digikey_executors(p.lookup, lambda: "call")[LOOKUP_DIGIKEY](args)
    assert result.failure["code"] == "arguments_unusable"
    p.lookup.assert_not_called()


def capture(order=None, xero=None, *, draft_cleanup_permitted=False):
    xero = xero or FakeXero()
    xero.contacts = ({"Name": "DigiKey", "ContactID": "c-1", "ContactStatus": "ACTIVE"},)
    handler, _ = handler_with_order(order)
    executors = xero_executors(xero, FakeMail(), lambda: "call", lambda *_: extracted(),
                                     digikey_lookup=provider(handler).lookup,
                                     draft_cleanup_permitted=draft_cleanup_permitted)
    return xero, executors[CAPTURE_DIGIKEY_BILL]


def test_capture_reuses_duplicate_commit_and_readback_without_pdf():
    xero, execute = capture()
    result = execute({"sales_order_number": "102748548"})
    assert result.values["completed"] is True
    assert result.capability_id == XERO
    assert CAPTURE_DIGIKEY_DEFINITION.output_schema.accepts(result.values)
    bill = xero.bills["bill-1"]
    assert bill["Status"] == "AUTHORISED"
    assert bill["InvoiceNumber"] == "135916797"
    assert bill["CurrencyCode"] == "ZAR"
    assert bill["Date"] == "2026-11-03"
    assert len(bill["LineItems"]) == 2
    assert bill["LineItems"][0]["Quantity"] == 10
    assert bill["LineItems"][1]["Description"] == "Shipping"
    assert bill["LineItems"][1]["UnitAmount"] == 460
    assert result.values["attached"] == ()
    assert xero.attachments["bill-1"] == []
    duplicate = execute({"sales_order_number": "102748548"})
    assert duplicate.values["returned_for"] == "needs_judgment"
    assert "duplicate_bill" in duplicate.values["detail"]
    assert xero.created == 1


@pytest.mark.parametrize("change", ["total", "missing_charges", "partial", "multiple", "tax", "date", "currency"])
def test_uncertain_order_never_writes(change):
    raw = complete_order()
    if change == "total": raw["TotalPrice"] = 2400
    if change == "missing_charges": raw.pop("ShippingCharges")
    if change == "partial": raw["LineItems"][0]["QuantityShipped"] = 9
    if change == "multiple":
        raw["LineItems"][0]["ItemShipments"].append({"InvoiceId": 333, "QuantityShipped": 1})
    if change == "tax": raw.update(SalesTax=10, TotalPrice=2474.91)
    if change == "date": raw["LineItems"][0]["ItemShipments"][0]["ShippedDate"] = ""
    if change == "currency": raw["Currency"] = ""
    xero, execute = capture(raw)
    result = execute({"sales_order_number": "102748548"})
    if change in ("total", "missing_charges"):
        assert result.failure["code"] == "source_mismatch"
    else:
        assert result.values["returned_for"] == "needs_judgment"
        assert result.outcome is ExecutionOutcome.AMBIGUOUS
    assert xero.created == 0


@pytest.mark.parametrize("authorise", [False, True])
@pytest.mark.parametrize("conflict", ["order_invoice", "shipment_invoices", "ship_dates"])
def test_conflicting_dates_return_for_judgment_without_xero_writes(authorise, conflict):
    raw = complete_order()
    shipments = raw["LineItems"][0]["ItemShipments"]
    if conflict == "order_invoice":
        raw["InvoiceDate"] = "2026-11-04"
        shipments[0]["InvoiceDate"] = "2026-11-03"
    else:
        shipments[0]["QuantityShipped"] = 5
        shipments.append({**shipments[0], "ShippedDate": "2026-11-04"})
        if conflict == "shipment_invoices":
            raw["InvoiceDate"] = "2026-11-03"
            shipments[0]["InvoiceDate"] = "2026-11-03"
            shipments[1]["InvoiceDate"] = "2026-11-04"
    xero, execute = capture(raw)
    xero.create_draft_bill = Mock(wraps=xero.create_draft_bill)
    xero.authorise_bill = Mock(wraps=xero.authorise_bill)
    result = execute({"sales_order_number": "102748548", "authorise": authorise})
    assert result.values["completed"] is False
    assert result.values["returned_for"] == "needs_judgment"
    assert "invoice_date_unresolved" in result.values["detail"]
    assert result.outcome is ExecutionOutcome.AMBIGUOUS
    xero.create_draft_bill.assert_not_called()
    xero.authorise_bill.assert_not_called()


@pytest.mark.parametrize("explicit_at", ["order", "one_shipment", "shipments", "both"])
def test_agreeing_invoice_dates_take_precedence_over_different_ship_dates(explicit_at):
    raw = complete_order()
    shipments = raw["LineItems"][0]["ItemShipments"]
    shipments[0]["QuantityShipped"] = 5
    shipments.append({**shipments[0], "ShippedDate": "2026-11-04"})
    if explicit_at in ("order", "both"):
        raw["InvoiceDate"] = "2026-11-05"
    if explicit_at in ("shipments", "both"):
        for shipment in shipments:
            shipment["InvoiceDate"] = "2026-11-05T12:00:00Z"
    if explicit_at == "one_shipment":
        shipments[0]["InvoiceDate"] = "2026-11-05"
    xero, execute = capture(raw)
    result = execute({"sales_order_number": "102748548"})
    assert result.values["completed"] is True
    assert xero.bills["bill-1"]["Date"] == "2026-11-05"
    assert xero.bills["bill-1"]["Status"] == "AUTHORISED"


@pytest.mark.parametrize("kind", ["supplier", "account", "conflicting_history", "mapping"])
def test_ambiguous_resolution_never_writes(kind):
    xero, execute = capture()
    if kind == "supplier": xero.contacts = (*xero.contacts, *xero.contacts)
    if kind == "account": xero.history = ()
    if kind == "conflicting_history":
        xero.history = (*xero.history, {"LineAmountTypes": "NoTax", "LineItems": [{"AccountCode": "999", "TaxType": "NONE"}]})
    if kind == "mapping": xero.accounts = ()
    result = execute({"sales_order_number": "102748548"})
    assert result.values["returned_for"] == "needs_judgment"
    assert result.outcome is ExecutionOutcome.AMBIGUOUS
    assert xero.created == 0


def test_rounding_difference_needs_judgment_before_write():
    raw = complete_order()
    raw["TotalPrice"] = 2464.92
    xero, execute = capture(raw)
    result = execute({"sales_order_number": "102748548"})
    assert result.failure["code"] == "source_mismatch"
    assert xero.created == 0


def shipped_rounding_order():
    # Exact quantities, prices and extensions from order 102748548 /
    # invoice 135916797. Product descriptions remain sanitized.
    raw = raw_order()
    raw.update(ShippingCharges="0", SalesTax="0", Subtotal="2464.9050", TotalPrice="2464.91")
    template = raw["LineItems"][0]
    raw["LineItems"] = []
    for quantity, unit, extension in (
        (250, "0.5488", "137.2000"),
        (250, "0.6671", "166.7750"),
        (250, "1.2455", "311.3750"),
        (250, "0.8117", "202.9250"),
        (250, "0.9054", "226.3500"),
        (1, "744.53", "744.53"),
        (250, "0.8117", "202.9250"),
        (250, "0.8709", "217.7250"),
        (250, "1.0204", "255.1000"),
    ):
        line = deepcopy(template)
        line.update(QuantityOrdered=quantity, QuantityShipped=quantity, UnitPrice=unit, TotalPrice=extension)
        line["ItemShipments"][0]["QuantityShipped"] = quantity
        raw["LineItems"].append(line)
    return raw


@pytest.mark.parametrize("identifier", ["sales_order_number", "invoice_number"])
@pytest.mark.parametrize("enabled", [False, True])
def test_reported_shipped_order_rounding_and_invoice_capture(identifier, enabled):
    raw = shipped_rounding_order()
    base, calls = handler_with_order(raw)
    def handle(request):
        if request.url.path.endswith("/orders"):
            return httpx.Response(200, json={"TotalOrders": 1, "Orders": [
                {"SalesOrders": [{"SalesOrderId": 102748548}]}]})
        return base(request)
    xero = FakeXero()
    xero.contacts = ({"Name": "DigiKey", "ContactID": "c-1", "ContactStatus": "ACTIVE"},)
    execute = xero_executors(xero, FakeMail(), lambda: "call", lambda *_: extracted(),
                                   digikey_lookup=provider(handle).lookup)[CAPTURE_DIGIKEY_BILL]
    args = {identifier: "135916797" if identifier == "invoice_number" else "102748548"}
    if enabled:
        args["rounding_adjustment"] = True
    assert CAPTURE_DIGIKEY_DEFINITION.input_schema.accepts(args)
    result = execute(args)
    order = result.values["invoice"]["order"]
    assert "135916797" in order["invoice_numbers"]
    assert len(order["line_items"]) == 9
    assert order["subtotal"] == "2464.9050"
    assert sum((Decimal(line["extended_price"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                for line in order["line_items"]), Decimal(0)) == Decimal("2464.93")
    if not enabled:
        assert "rounding_allocation_unresolved" in result.values["detail"]
        assert result.values["returned_for"] == "needs_judgment"
        assert xero.created == 0
        return
    assert result.values["completed"] is True
    bill = xero.bills["bill-1"]
    assert bill["Total"] == "2464.91"
    adjustments = [line for line in bill["LineItems"] if line["Description"] == "Rounding adjustment"]
    assert len(adjustments) == 1
    assert adjustments[0]["UnitAmount"] == -0.02
    ordinary = [line for line in bill["LineItems"] if line["Description"] != "Rounding adjustment"]
    assert len(ordinary) == 9
    for source, sent in zip(raw["LineItems"], ordinary):
        assert Decimal(str(sent["UnitAmount"])) == Decimal(source["UnitPrice"]).quantize(Decimal("0.0001"))
        assert Decimal(str(sent["Quantity"])) == Decimal(source["QuantityOrdered"])
    amounts = [(Decimal(str(line["Quantity"])) * Decimal(str(line["UnitAmount"]))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP) for line in bill["LineItems"]]
    assert sum(amounts[:-1]) == Decimal("2464.93")
    assert sum(amounts) == Decimal("2464.91")
    assert bill["LineAmountTypes"] == "NoTax"
    assert all(line["TaxType"] == "NONE" and line["TaxAmount"] == 0 for line in bill["LineItems"])
    for key in ("AccountCode", "TaxType", "TaxAmount"):
        assert adjustments[0][key] == bill["LineItems"][0][key]
    assert not any(line["Description"] == "Shipping" for line in bill["LineItems"])


@pytest.mark.parametrize("difference", ["0", "0.02", "-0.02", "0.05", "-0.05", "0.06", "-0.06"])
def test_rounding_adjustment_bounds(difference):
    raw = shipped_rounding_order()
    raw["TotalPrice"] = str(Decimal("2464.93") + Decimal(difference))
    xero, execute = capture(raw)
    result = execute({"sales_order_number": "102748548", "rounding_adjustment": True})
    if Decimal(raw["TotalPrice"]) != Decimal("2464.91"):
        assert result.failure["code"] == "source_mismatch"
        assert xero.created == 0
    else:
        assert result.values["completed"] is True
        bill = xero.bills["bill-1"]
        assert Decimal(bill["Total"]) == Decimal(raw["TotalPrice"])
        adjustments = [line for line in bill["LineItems"] if line["Description"] == "Rounding adjustment"]
        assert len(adjustments) == bool(Decimal(difference))


@pytest.mark.parametrize("authorise", [False, True])
@pytest.mark.parametrize("change", ["price", "small_price", "submitted_precision", "subtotal", "extension"])
def test_non_rounding_difference_fails_before_any_write(change, authorise):
    raw = shipped_rounding_order()
    if change == "price":
        raw["LineItems"][0]["UnitPrice"] = "0.5588"
    elif change == "small_price":
        # The old tolerance could hide this extra cent on a quantity-one line.
        raw["LineItems"][5]["UnitPrice"] = "744.54"
    elif change == "submitted_precision":
        # The discrepancy fits both bounds, but comes from price truncation
        # to four decimals, not cent rounding of the submitted extensions.
        raw = rounding_order(10, "1.0000049", quantity=1000)
    elif change == "subtotal":
        raw["Subtotal"] = "2464.9150"
    xero, execute = capture(raw)
    if change == "extension":
        order = normalize_order(raw)
        order["line_items"][5]["extended_price"] = "744.54"
        execute = xero_executors(xero, FakeMail(), lambda: "call",
                                      digikey_lookup=lambda **_: order)[CAPTURE_DIGIKEY_BILL]
    assert_source_mismatch_without_write(xero, execute, authorise=authorise)


def rounding_order(count, unit, *, quantity=1, shipping="0"):
    raw = raw_order()
    template = raw["LineItems"][0]
    subtotal = Decimal(count) * Decimal(quantity) * Decimal(unit)
    raw.update(ShippingCharges=shipping, SalesTax="0", Subtotal=str(subtotal),
               TotalPrice=str((subtotal + Decimal(shipping)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)))
    raw["LineItems"] = []
    for _ in range(count):
        line = deepcopy(template)
        line.update(QuantityOrdered=quantity, QuantityShipped=quantity, UnitPrice=unit)
        line["ItemShipments"][0]["QuantityShipped"] = quantity
        raw["LineItems"].append(line)
    return raw


def assert_source_mismatch_without_write(xero, execute, *, authorise=True):
    xero.create_draft_bill = Mock(wraps=xero.create_draft_bill)
    xero.authorise_bill = Mock(wraps=xero.authorise_bill)
    result = execute({"sales_order_number": "102748548", "rounding_adjustment": True,
                      "authorise": authorise})
    assert result.state is CapabilityResultState.FAILED
    assert result.failure == {"code": "source_mismatch"}
    xero.create_draft_bill.assert_not_called()
    xero.authorise_bill.assert_not_called()
    assert xero.created == 0
    assert not xero.bills


@pytest.mark.parametrize("count,unit,shipping,expected", [
    (2, "1.0050", "0", "-0.01"),  # Exactly 0.005 per source line.
    (2, "1.0049", "0", "0.01"),   # Positive residue at the same bound.
    (9, "1.0050", "0", "-0.04"),  # Odd line count: bound is 0.045, not 0.05.
    (10, "1.0050", "0", "-0.05"), # Exactly the absolute cap.
    (12, "1.0050", "0", None),     # Pure rounding, but exceeds 0.05.
    (12, "1.0049", "0", None),     # Positive residue also exceeds the cap.
    (1, "1.0049", "0.0049", None), # Exceeds the per-source-line bound.
])
@pytest.mark.parametrize("authorise", [False, True])
def test_proven_rounding_respects_line_count_and_absolute_bounds(count, unit, shipping, expected, authorise):
    xero, execute = capture(rounding_order(count, unit, shipping=shipping))
    if expected is None:
        assert_source_mismatch_without_write(xero, execute, authorise=authorise)
        return
    result = execute({"sales_order_number": "102748548", "rounding_adjustment": True,
                      "authorise": authorise})
    assert result.values["completed"] is True
    bill = xero.bills["bill-1"]
    adjustments = [line for line in bill["LineItems"] if line["Description"] == "Rounding adjustment"]
    assert len(adjustments) == 1
    assert Decimal(str(adjustments[0]["UnitAmount"])) == Decimal(expected)
    assert Decimal(bill["Total"]) == Decimal(rounding_order(count, unit, shipping=shipping)["TotalPrice"])


@pytest.mark.parametrize("line_amount_types", ["NoTax", "Exclusive"])
def test_rounding_prediction_includes_shipping_and_shared_no_tax_resolution(line_amount_types):
    raw = shipped_rounding_order()
    raw.update(ShippingCharges="10.1256", TotalPrice="2475.03")
    xero, execute = capture(raw)
    xero.history = ({"LineAmountTypes": line_amount_types,
                     "LineItems": [{"AccountCode": "310", "TaxType": "NONE"}]},)
    result = execute({"sales_order_number": "102748548", "rounding_adjustment": True})
    assert result.values["completed"] is True
    bill = xero.bills["bill-1"]
    assert bill["Total"] == "2475.03"
    # The source states zero tax, so shared coding selects NoTax even when
    # supplier history used Exclusive amounts.
    assert bill["LineAmountTypes"] == "NoTax"
    adjustment = next(line for line in bill["LineItems"] if line["Description"] == "Rounding adjustment")
    shipping = next(line for line in bill["LineItems"] if line["Description"] == "Shipping")
    assert Decimal(str(adjustment["UnitAmount"])) == Decimal("-0.03")
    assert Decimal(str(shipping["UnitAmount"])) == Decimal("10.1256")
    assert all(line["TaxType"] == "NONE" and line["TaxAmount"] == 0 for line in bill["LineItems"])


@pytest.mark.parametrize("field", ["ShippingCharges", "SalesTax"])
@pytest.mark.parametrize("value", [None, "", "garbage"])
def test_optional_charges_and_invalid_nonempty_values(field, value):
    raw = fractional_order()
    if value is None:
        raw.pop(field)
    else:
        raw[field] = value
    xero, execute = capture(raw)
    result = execute({"sales_order_number": "102748548"})
    if value == "garbage":
        assert result.failure["code"] == "response_invalid"
        assert xero.created == 0
    else:
        assert result.values["completed"] is True
        assert len(xero.bills["bill-1"]["LineItems"]) == 1


def test_normalized_empty_charges_and_nonempty_garbage():
    from alx.tools.digikey import bill_invoice

    order = normalize_order(fractional_order())
    order.update(shipping="", tax="")
    invoice, lines, problems = bill_invoice(order)
    assert not problems
    assert invoice["tax_amount"] == "0"
    assert len(lines) == 1
    for field in ("shipping", "tax"):
        _, _, problems = bill_invoice({**order, field: "garbage"}, rounding_adjustment=True)
        assert "billing_values_unresolved" in problems


def test_rounding_adjustment_draft_resumes_through_shared_commit():
    xero, execute = capture(shipped_rounding_order())
    args = {"sales_order_number": "102748548", "rounding_adjustment": True}
    assert execute({**args, "authorise": False}).values["bill"]["status"] == "DRAFT"
    result = execute(args)
    assert result.values["completed"] is True
    assert "resumed_existing_draft" in result.values["steps"]
    assert xero.created == 1


@pytest.mark.parametrize("value", ["true", 1, None])
def test_rounding_option_must_be_boolean(value):
    xero, execute = capture()
    result = execute({"sales_order_number": "102748548", "rounding_adjustment": value})
    assert result.failure["code"] == "arguments_unusable"
    assert xero.created == 0


def test_matching_draft_is_resumed_through_shared_commit():
    xero, execute = capture()
    first = execute({"sales_order_number": "102748548", "authorise": False})
    assert first.values["bill"]["status"] == "DRAFT"
    second = execute({"sales_order_number": "102748548"})
    assert second.values["bill"]["status"] == "AUTHORISED"
    assert "resumed_existing_draft" in second.values["steps"]
    assert xero.created == 1


def fractional_order():
    raw = complete_order()
    raw.update(ShippingCharges=0, TotalPrice=1.00)
    line = raw["LineItems"][0]
    line.update(QuantityOrdered=3, QuantityShipped=3, UnitPrice=0.3333, TotalPrice=1.00)
    line["ItemShipments"][0]["QuantityShipped"] = 3
    return raw


@pytest.mark.parametrize("stored_line_amount", [False, True])
def test_fractional_unit_price_draft_retry_preserves_precision(stored_line_amount):
    xero, execute = capture(fractional_order())
    first = execute({"sales_order_number": "102748548", "authorise": False})
    assert first.values["bill"]["status"] == "DRAFT"
    bill = xero.bills["bill-1"]
    assert bill["Total"] == "1.00"
    assert bill["LineItems"][0]["UnitAmount"] == 0.3333
    if stored_line_amount:
        bill["LineItems"][0]["LineAmount"] = 1.00

    second = execute({"sales_order_number": "102748548", "authorise": True})
    assert second.values["completed"] is True
    assert second.values["bill"]["status"] == "AUTHORISED"
    assert "resumed_existing_draft" in second.values["steps"]
    assert xero.created == 1


@pytest.mark.parametrize("change", ["total", "extension", "unit_price", "quantity", "tax"])
def test_fractional_price_retry_rejects_different_draft_values(change):
    xero, execute = capture(fractional_order())
    first = execute({"sales_order_number": "102748548", "authorise": False})
    assert first.values["bill"]["status"] == "DRAFT"
    bill = xero.bills["bill-1"]
    line = bill["LineItems"][0]
    if change == "total":
        bill["Total"] = "1.01"
    if change == "extension":
        line["LineAmount"] = 1.01
    if change == "unit_price":
        line["UnitAmount"] = 0.3334  # Same rounded extension, different price.
    if change == "quantity":
        line.update(Quantity=1, UnitAmount=0.9999)
    if change == "tax":
        line["TaxAmount"] = 0.01
    xero.authorise_bill = Mock(wraps=xero.authorise_bill)
    result = execute({"sales_order_number": "102748548", "authorise": True})
    assert result.values["completed"] is False
    assert result.values["returned_for"] == "needs_judgment"
    assert bill["Status"] == "DRAFT"
    assert xero.created == 1
    xero.authorise_bill.assert_not_called()


def bill_call_owners(source):
    tree = ast.parse(source)
    owners = {}
    def walk(node, owner=""):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = node.name
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id if isinstance(node.func, ast.Name) else ""
            if name in ("create_draft_bill", "authorise_bill", "_commit_decided_bill", "_capture_structured_invoice"):
                owners.setdefault(name, []).append(owner)
        for child in ast.iter_child_nodes(node): walk(child, owner)
    walk(tree)
    return owners


def test_one_writer_and_shared_resolution_core_with_mutation():
    source = (ROOT / "src/alx/tools/xero.py").read_text()
    expected = {"create_draft_bill": ["_commit_decided_bill"], "authorise_bill": ["_commit_decided_bill"],
                "_commit_decided_bill": ["_capture_structured_invoice"],
                "_capture_structured_invoice": ["capture_invoice", "capture_digikey"]}
    assert bill_call_owners(source) == expected
    mutated = source + "\ndef alternative(account, payload):\n    account.create_draft_bill(payload)\n"
    assert bill_call_owners(mutated) != expected
    # Both use the exact same resolution functions; no duplicate resolving/writing module.
    tree = ast.parse(source)
    core = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_capture_structured_invoice")
    assert {n.func.id for n in ast.walk(core) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)} >= {"resolve_supplier", "prior_coding"}
    other_source = (ROOT / "src/alx/tools/digikey.py").read_text()
    assert bill_call_owners(other_source) == {}


def test_runtime_independent_of_pdf_extractor_and_authority(tmp_path):
    read_provider = build_digikey_provider(ENV)
    assert read_provider is not None
    assert build_digikey_provider({}) is None
    assert "private" not in repr(DigiKeySettings.from_environment(ENV))
    for unattended in (False, True):
        runtime = build_xero_runtime(xero_settings(unattended_bill_writes=unattended), tmp_path,
                                     FakeMail(), lambda: "call", digikey_lookup=read_provider.lookup)
        offered = {item.value for item in runtime.actions}
        assert {CAPTURE_DIGIKEY_BILL, LOOKUP_DIGIKEY} <= offered
        assert "capture_invoice" not in offered
        assert needs_approval(runtime, CAPTURE_DIGIKEY_BILL) is not unattended
        # The order read is a read: never approval, its own permission kept.
        assert not needs_approval(runtime, LOOKUP_DIGIKEY)
        assert DIGIKEY_ORDER_READ_PERMISSION in runtime.policies[XERO].permission_references
    assert XeroAction.CAPTURE_DIGIKEY_BILL in BILL_EXECUTION_ACTIONS
    assert CAPTURE_DIGIKEY_DEFINITION.side_effect is SideEffect.EFFECTFUL


@pytest.mark.parametrize("rounding_adjustment", [False, True])
def test_four_decimal_prices_need_no_adjustment_when_lines_reconcile(rounding_adjustment):
    # A single four-decimal price whose cent-rounded total already reconciles.
    raw = raw_order()
    raw.update(TotalPrice="9.05", ShippingCharges=0, SalesTax=0)
    line = raw["LineItems"][0]
    line.update(QuantityOrdered=10, QuantityShipped=10, UnitPrice="0.9054", TotalPrice="9.054")
    xero, execute = capture(raw)
    result = execute({"sales_order_number": "102748548", "rounding_adjustment": rounding_adjustment})
    assert result.values["completed"] is True
    bill = xero.bills["bill-1"]
    assert bill["Total"] == "9.05"
    assert len(bill["LineItems"]) == 1
    assert bill["LineItems"][0]["UnitAmount"] == 0.9054
    assert bill["LineItems"][0]["TaxType"] == "NONE"
    assert bill["LineItems"][0]["TaxAmount"] == 0


@pytest.mark.parametrize("line_amount_types,unit,tax,expected", [
    ("NoTax", "0.9054", "0", "9.05"),
    ("NoTax", "0.9055", "0", "9.06"),
    ("Exclusive", "0.9055", "1.36", "10.42"),
    ("Inclusive", "0.9055", "1.18", "9.06"),
    ("NoTax", "0.905449", "0", "9.05"),
])
def test_shared_payload_calculates_xero_four_dp_line_rounding(line_amount_types, unit, tax, expected):
    from alx.tools.xero import _draft_payload
    xero = FakeXero()
    xero.tax_rates = ({"TaxType": "INPUT", "Status": "ACTIVE"},)
    payload, total = _draft_payload({
        "contact_id": "c-1", "invoice_number": "test", "date": "2026-11-03",
        "due_date": "2026-11-03", "currency": "ZAR", "reference": "test",
        "line_amount_types": line_amount_types, "expected_total": expected,
        "line_items": [{"description": "Synthetic capacitor", "quantity": "10",
                        "unit_amount": unit, "tax_amount": tax, "account_code": "310",
                        "tax_type": "NONE" if line_amount_types == "NoTax" else "INPUT"}],
    }, xero)
    assert total == Decimal(expected)
    assert Decimal(str(payload["LineItems"][0]["UnitAmount"])) == Decimal(unit).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP)


def test_shared_commit_rejects_source_mismatch_before_write():
    xero = FakeXero()
    executors = xero_executors(xero, FakeMail(), lambda: "call",
                                     lambda *_: extracted(subtotal="179.99"))
    result = executors["capture_invoice"](arguments())
    assert result.failure["code"] == "source_mismatch"
    assert xero.created == 0
    assert not xero.bills


@pytest.mark.parametrize("stage", ["create", "read", "final"])
@pytest.mark.parametrize("authorise", [False, True])
@pytest.mark.parametrize("delete_permitted", [False, True])
@pytest.mark.parametrize("source", ["pdf", "digikey"])
def test_created_total_cleanup_respects_separate_runtime_delete_authority(
    tmp_path, monkeypatch, stage, authorise, delete_permitted, source
):
    xero = FakeXero()
    if source == "digikey":
        xero.contacts = ({"Name": "DigiKey", "ContactID": "c-1", "ContactStatus": "ACTIVE"},)
    monkeypatch.setattr("alx.bootstrap.xero.XeroAccountingAdapter", lambda *_: xero)
    handler, _ = handler_with_order()
    runtime = build_xero_runtime(
        xero_settings(unattended_bill_writes=True, unattended_bill_deletes=delete_permitted),
        tmp_path, FakeMail(), lambda: "call", lambda *_: extracted(),
        digikey_lookup=provider(handler).lookup,
    )
    capability = "capture_invoice" if source == "pdf" else CAPTURE_DIGIKEY_BILL
    assert not needs_approval(runtime, capability)
    assert needs_approval(runtime, "delete_draft_bill") is not delete_permitted
    create, read = xero.create_draft_bill, xero.read_bill
    reads = 0

    def wrong_create(payload):
        stored = create(payload)
        if stage == "create":
            stored["Total"] = "2467.04"
        return dict(stored)

    def wrong_read(invoice_id):
        nonlocal reads
        reads += 1
        stored = read(invoice_id)
        if (stage == "read" or (stage == "final" and reads == 2)) and stored["Status"] == "DRAFT":
            stored["Total"] = "2467.04"
        return stored

    xero.create_draft_bill = wrong_create
    xero.read_bill = wrong_read
    xero.delete_draft_bill = Mock(wraps=xero.delete_draft_bill)
    xero.authorise_bill = Mock(wraps=xero.authorise_bill)
    args = arguments(authorise=authorise) if source == "pdf" else {
        "sales_order_number": "102748548", "authorise": authorise}
    result = runtime.executors[XERO](xero_arguments(capability, args))
    if stage == "final" and authorise:
        assert result.values["completed"] is True
        xero.delete_draft_bill.assert_not_called()
        return
    assert result.state is CapabilityResultState.FAILED
    assert result.failure["code"] == "source_mismatch"
    xero.authorise_bill.assert_not_called()
    if delete_permitted:
        xero.delete_draft_bill.assert_called_once_with("bill-1")
        assert xero.bills["bill-1"]["Status"] == "DELETED"
    else:
        xero.delete_draft_bill.assert_not_called()
        assert xero.bills["bill-1"]["Status"] == "DRAFT"
        assert result.failure["draft_retained"] is True
        assert result.failure["invoice_id"] == "bill-1"
        assert result.failure["total"] == "2467.04"


@pytest.mark.parametrize("stage", ["create", "read", "final"])
@pytest.mark.parametrize("authorise", [False, True])
def test_new_mismatched_draft_is_deleted_before_return_or_authorisation(stage, authorise):
    xero, execute = capture(draft_cleanup_permitted=True)
    create = xero.create_draft_bill
    read = xero.read_bill
    reads = 0

    def wrong_create(payload):
        stored = create(payload)
        if stage == "create":
            stored["Total"] = "2467.04"
        return dict(stored)

    def wrong_read(invoice_id):
        nonlocal reads
        reads += 1
        stored = read(invoice_id)
        if (stage == "read" or (stage == "final" and reads == 2)) and stored["Status"] == "DRAFT":
            stored["Total"] = "2467.04"
        return stored

    xero.create_draft_bill = wrong_create
    xero.read_bill = wrong_read
    xero.authorise_bill = Mock(wraps=xero.authorise_bill)
    result = execute({"sales_order_number": "102748548", "authorise": authorise})
    if stage == "final" and authorise:
        # The draft matched before authorisation; this case is no longer a draft.
        assert result.values["completed"] is True
    else:
        assert result.failure["code"] == "source_mismatch"
        assert xero.bills["bill-1"]["Status"] == "DELETED"
        xero.authorise_bill.assert_not_called()


@pytest.mark.parametrize("failure", ["exception", "unconfirmed"])
def test_draft_cleanup_failure_is_explicit_and_retains_bill_identity(failure):
    from alx.contracts import XeroAccessError
    xero, execute = capture(draft_cleanup_permitted=True)
    create = xero.create_draft_bill

    def wrong_create(payload):
        stored = create(payload)
        stored["Total"] = "2467.04"
        return stored

    def refuse_delete(invoice_id):
        if failure == "exception":
            raise XeroAccessError("connection_failed")
        return xero.bills[invoice_id]

    xero.create_draft_bill = wrong_create
    xero.delete_draft_bill = refuse_delete
    xero.authorise_bill = Mock(wraps=xero.authorise_bill)
    result = execute({"sales_order_number": "102748548"})
    assert result.values["completed"] is False
    assert "draft_cleanup_failed" in result.values["detail"]
    assert result.values["bill"]["invoice_id"] == "bill-1"
    assert xero.bills["bill-1"]["Status"] == "DRAFT"
    xero.authorise_bill.assert_not_called()


def test_preexisting_mismatched_draft_is_never_deleted():
    xero, execute = capture()
    assert execute({"sales_order_number": "102748548", "authorise": False}).values["completed"]
    xero.bills["bill-1"]["Total"] = "2467.04"
    xero.delete_draft_bill = Mock(wraps=xero.delete_draft_bill)
    result = execute({"sales_order_number": "102748548"})
    assert result.values["completed"] is False
    assert xero.created == 1
    assert xero.bills["bill-1"]["Status"] == "DRAFT"
    xero.delete_draft_bill.assert_not_called()
