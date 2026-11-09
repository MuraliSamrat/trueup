import json
from decimal import Decimal

import httpx
import pytest

from app.paypal import (
    LineItem,
    PayPalError,
    authorize_order,
    capture_authorization,
    capture_order,
    create_authorize_order,
    create_supplemental_order,
    reauthorize,
    usd,
    void_authorization,
)


def body(route):
    return json.loads(route.calls.last.request.content)


def test_create_authorize_order_payload(client, mock_api):
    route = mock_api.post("/v2/checkout/orders").mock(
        return_value=httpx.Response(200, json={"id": "ORD1", "status": "PAYER_ACTION_REQUIRED",
                                               "links": [{"rel": "payer-action", "href": "https://approve/ORD1"}]})
    )
    o = create_authorize_order(client, usd("60"), "Plumber visit", "job-1", "https://r", "https://c")
    b = body(route)
    assert b["intent"] == "AUTHORIZE"
    assert b["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "60.00"}
    assert b["purchase_units"][0]["custom_id"] == "job-1"
    ctx = b["payment_source"]["paypal"]["experience_context"]
    assert ctx["shipping_preference"] == "NO_SHIPPING" and ctx["return_url"] == "https://r"
    assert (o.id, o.approve_url) == ("ORD1", "https://approve/ORD1")


def test_supplemental_order_is_itemized_and_totals_match(client, mock_api):
    route = mock_api.post("/v2/checkout/orders").mock(
        return_value=httpx.Response(200, json={"id": "SUP1", "status": "PAYER_ACTION_REQUIRED", "links": []})
    )
    items = [LineItem(name="Washer", unit_amount=usd("6")), LineItem(name="Faucet cartridge", unit_amount=usd("45"))]
    create_supplemental_order(client, items, "Price change for job-1", "job-1:sup", "https://r", "https://c")
    pu = body(route)["purchase_units"][0]
    assert body(route)["intent"] == "CAPTURE"
    assert pu["amount"]["value"] == "51.00"
    assert pu["amount"]["breakdown"]["item_total"]["value"] == "51.00"
    assert [i["name"] for i in pu["items"]] == ["Washer", "Faucet cartridge"]


def test_supplemental_order_needs_items(client):
    with pytest.raises(ValueError):
        create_supplemental_order(client, [], "x", "y", "r", "c")


def test_authorize_order_returns_hold(client, mock_api):
    mock_api.post("/v2/checkout/orders/ORD1/authorize").mock(
        return_value=httpx.Response(201, json={"id": "ORD1", "status": "COMPLETED", "purchase_units": [{
            "custom_id": "job-1",
            "payments": {"authorizations": [{"id": "AUTH1", "status": "CREATED",
                                             "amount": {"currency_code": "USD", "value": "60.00"},
                                             "expiration_time": "2026-12-08T08:36:12Z"}]}}]})
    )
    a = authorize_order(client, "ORD1")
    assert (a.id, a.status, a.amount.amount, a.custom_id) == ("AUTH1", "CREATED", Decimal("60.00"), "job-1")
    assert a.expiration_time.year == 2026


def test_capture_authorization_partial_and_final(client, mock_api):
    route = mock_api.post("/v2/payments/authorizations/AUTH1/capture").mock(
        return_value=httpx.Response(201, json={"id": "CAP1", "status": "COMPLETED", "final_capture": False,
                                               "amount": {"currency_code": "USD", "value": "20.00"}})
    )
    c = capture_authorization(client, "AUTH1", usd("20"), final_capture=False, note="Washer replaced")
    assert body(route) == {"amount": {"currency_code": "USD", "value": "20.00"}, "final_capture": False,
                           "note_to_payer": "Washer replaced"}
    assert (c.id, c.amount.amount, c.final_capture) == ("CAP1", Decimal("20.00"), False)


def test_capture_above_cap_raises_typed_error(client, mock_api):
    mock_api.post("/v2/payments/authorizations/AUTH1/capture").mock(
        return_value=httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY", "debug_id": "d1",
                                               "details": [{"issue": "MAX_CAPTURE_AMOUNT_EXCEEDED"}]})
    )
    with pytest.raises(PayPalError) as exc:
        capture_authorization(client, "AUTH1", usd("120"))
    assert exc.value.issue == "MAX_CAPTURE_AMOUNT_EXCEEDED"


def test_void_returns_none_on_204(client, mock_api):
    route = mock_api.post("/v2/payments/authorizations/AUTH3/void").mock(return_value=httpx.Response(204))
    assert void_authorization(client, "AUTH3") is None
    assert route.called


def test_reauthorize(client, mock_api):
    mock_api.post("/v2/payments/authorizations/AUTH1/reauthorize").mock(
        return_value=httpx.Response(201, json={"id": "AUTH9", "status": "CREATED",
                                               "amount": {"currency_code": "USD", "value": "60.00"}})
    )
    assert reauthorize(client, "AUTH1", usd("60")).id == "AUTH9"


def test_capture_order_for_supplemental(client, mock_api):
    mock_api.post("/v2/checkout/orders/SUP1/capture").mock(
        return_value=httpx.Response(201, json={"id": "SUP1", "status": "COMPLETED", "purchase_units": [{
            "payments": {"captures": [{"id": "CAP9", "status": "COMPLETED",
                                       "amount": {"currency_code": "USD", "value": "51.00"}}]}}]})
    )
    assert capture_order(client, "SUP1").amount.amount == Decimal("51.00")
