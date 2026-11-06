"""HTTP-mocked v4 orders and the single structured supplier-bill path."""

import ast
from copy import deepcopy
from decimal import Decimal
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alx.bootstrap.digikey import build_digikey_runtime
from alx.bootstrap.xero import build_xero_runtime, BILL_EXECUTION_CAPABILITIES
from alx.config.digikey import DigiKeySettings
from alx.contracts import CapabilityResultState, ExecutionOutcome, SideEffect
from alx.providers.digikey import DigiKeyError, DigiKeyProvider, normalize_order
from alx.tools.digikey import LOOKUP_DEFINITION, LOOKUP_DIGIKEY_ORDER, CAPTURE_DIGIKEY_ORDER_BILL, build_digikey_executors
from alx.tools.xero import CAPTURE_DIGIKEY_DEFINITION, build_xero_executors
from test_invoice_capture import FakeXero, FakeMail, arguments, extracted
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
    execute = build_digikey_executors(provider(handler), lambda: "call")[LOOKUP_DIGIKEY_ORDER]
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
        execute = build_xero_executors(xero, FakeMail(), lambda: "call",
                                       digikey_lookup=p.lookup)[CAPTURE_DIGIKEY_ORDER_BILL]
    else:
        execute = build_digikey_executors(p, lambda: "call")[LOOKUP_DIGIKEY_ORDER]
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
        execute = build_xero_executors(xero, FakeMail(), lambda: "call",
                                       digikey_lookup=p.lookup)[CAPTURE_DIGIKEY_ORDER_BILL]
    else:
        execute = build_digikey_executors(p, lambda: "call")[LOOKUP_DIGIKEY_ORDER]
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
    result = build_digikey_executors(p, lambda: "call")[LOOKUP_DIGIKEY_ORDER]({"sales_order_number": "1"})
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
    result = build_digikey_executors(p, lambda: "call")[LOOKUP_DIGIKEY_ORDER](args)
    assert result.failure["code"] == "arguments_unusable"
    p.lookup.assert_not_called()


def capture(order=None, xero=None):
    xero = xero or FakeXero()
    xero.contacts = ({"Name": "DigiKey", "ContactID": "c-1", "ContactStatus": "ACTIVE"},)
    handler, _ = handler_with_order(order)
    executors = build_xero_executors(xero, FakeMail(), lambda: "call", lambda *_: extracted(),
                                     digikey_lookup=provider(handler).lookup)
    return xero, executors[CAPTURE_DIGIKEY_ORDER_BILL]


def test_capture_reuses_duplicate_commit_and_readback_without_pdf():
    xero, execute = capture()
    result = execute({"sales_order_number": "102748548"})
    assert result.values["completed"] is True
    assert result.capability_id == CAPTURE_DIGIKEY_ORDER_BILL
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
    assert result.values["returned_for"] == "needs_judgment"
    assert "rounding_allocation_unresolved" in result.values["detail"]
    assert xero.created == 0


def shipped_rounding_order():
    # Reported IDs, total and four-decimal extensions, with synthetic goods.
    raw = raw_order()
    raw.update(ShippingCharges="", SalesTax="")
    template = raw["LineItems"][0]
    raw["LineItems"] = []
    for extension in ("166.7750", "311.3750", "100.0150", "200.0150", "1686.7300"):
        line = deepcopy(template)
        line.update(QuantityOrdered=1, QuantityShipped=1, UnitPrice=extension)
        line["ItemShipments"][0]["QuantityShipped"] = 1
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
    execute = build_xero_executors(xero, FakeMail(), lambda: "call", lambda *_: extracted(),
                                   digikey_lookup=provider(handle).lookup)[CAPTURE_DIGIKEY_ORDER_BILL]
    args = {identifier: "135916797" if identifier == "invoice_number" else "102748548"}
    if enabled:
        args["rounding_adjustment"] = True
    assert CAPTURE_DIGIKEY_DEFINITION.input_schema.accepts(args)
    result = execute(args)
    order = result.values["invoice"]["order"]
    assert "135916797" in order["invoice_numbers"]
    assert sum((Decimal(line["extended_price"]).quantize(Decimal("0.01"))
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
    for key in ("AccountCode", "TaxType", "TaxAmount"):
        assert adjustments[0][key] == bill["LineItems"][0][key]
    assert not any(line["Description"] == "Shipping" for line in bill["LineItems"])


@pytest.mark.parametrize("difference", ["0", "0.02", "-0.02", "0.05", "-0.05", "0.06", "-0.06"])
def test_rounding_adjustment_bounds(difference):
    raw = shipped_rounding_order()
    raw["TotalPrice"] = str(Decimal("2464.93") + Decimal(difference))
    xero, execute = capture(raw)
    result = execute({"sales_order_number": "102748548", "rounding_adjustment": True})
    if abs(Decimal(difference)) > Decimal("0.05"):
        assert result.values["returned_for"] == "needs_judgment"
        assert "rounding_allocation_unresolved" in result.values["detail"]
        assert xero.created == 0
    else:
        assert result.values["completed"] is True
        bill = xero.bills["bill-1"]
        assert Decimal(bill["Total"]) == Decimal(raw["TotalPrice"])
        adjustments = [line for line in bill["LineItems"] if line["Description"] == "Rounding adjustment"]
        assert len(adjustments) == bool(Decimal(difference))


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
    read_runtime = build_digikey_runtime(ENV, lambda: "call")
    assert read_runtime is not None
    assert build_digikey_runtime({}, lambda: "call") is None
    assert "private" not in repr(DigiKeySettings.from_environment(ENV))
    for unattended in (False, True):
        runtime = build_xero_runtime(xero_settings(unattended_bill_writes=unattended), tmp_path,
                                     FakeMail(), lambda: "call", digikey_lookup=read_runtime.provider.lookup)
        assert CAPTURE_DIGIKEY_ORDER_BILL in runtime.executors
        assert "capture_supplier_invoice" not in runtime.executors
        assert runtime.policies[CAPTURE_DIGIKEY_ORDER_BILL].approval_required is not unattended
    assert CAPTURE_DIGIKEY_ORDER_BILL in BILL_EXECUTION_CAPABILITIES
    assert CAPTURE_DIGIKEY_DEFINITION.side_effect is SideEffect.EFFECTFUL
