import json
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from app.engine import (
    Action,
    ApprovalError,
    Mandate,
    complete_price_change,
    decide,
    decline_price_change,
    request_price_change,
    settle,
    supplemental_items,
)
from app.paypal.models import LineItem, usd

T0 = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
HOLD = usd("60")
M = Mandate(mandate_id="m1", customer_ref="agent-abc", created_at=T0)
VISIT = LineItem(name="Plumber visit", unit_amount=usd("60"), category="visit")
WASHER = LineItem(name="Washer", unit_amount=usd("6"), category="parts", added=True)
CART = LineItem(name="Faucet cartridge", unit_amount=usd("45"), category="parts", added=True)
AUTH_CAPTURE = "/v2/payments/authorizations/AUTH1/capture"


def capture_reply(request):
    b = json.loads(request.content)
    return httpx.Response(201, json={"id": "CAP", "status": "COMPLETED", "amount": b["amount"],
                                     "final_capture": b["final_capture"]})


def test_void(client, mock_api):
    route = mock_api.post("/v2/payments/authorizations/AUTH1/void").mock(return_value=httpx.Response(204))
    s = settle(client, "AUTH1", decide(HOLD, [VISIT], M, cancelled=True, now=T0), "inv-1")
    assert s.voided and s.charged.amount == 0 and route.called


def test_auto_capture_sends_amount_and_prefer_header(client, mock_api):
    route = mock_api.post(AUTH_CAPTURE).mock(side_effect=capture_reply)
    d = decide(HOLD, [VISIT, WASHER], M, now=T0)
    s = settle(client, "AUTH1", d, "inv-1", note="Visit + washer")
    req = route.calls.last.request
    assert json.loads(req.content)["amount"]["value"] == "66.00"
    assert req.headers["Prefer"] == "return=representation"
    assert s.action == Action.AUTO_CAPTURE and s.captures[0].amount.amount == Decimal("66.00")


def test_capture_within_charges_less(client, mock_api):
    route = mock_api.post(AUTH_CAPTURE).mock(side_effect=capture_reply)
    cheap = LineItem(name="Plumber visit (tighten pipe)", unit_amount=usd("40"))
    s = settle(client, "AUTH1", decide(HOLD, [cheap], M, now=T0), "inv-1")
    assert json.loads(route.calls.last.request.content)["amount"]["value"] == "40.00"
    assert s.charged.amount == Decimal("40.00")


def test_settle_refuses_needs_approval(client):
    with pytest.raises(ApprovalError):
        settle(client, "AUTH1", decide(HOLD, [VISIT, WASHER, CART], M, now=T0), "inv-1")


def test_price_change_requires_owner(client):
    d = decide(HOLD, [VISIT, WASHER, CART], M, now=T0)
    with pytest.raises(ApprovalError):
        request_price_change(client, d, [VISIT, WASHER, CART], "job-1", "r", "c", owner_approved=False)


def test_price_change_only_for_needs_approval(client):
    d = decide(HOLD, [VISIT, WASHER], M, now=T0)
    with pytest.raises(ApprovalError):
        request_price_change(client, d, [VISIT, WASHER], "job-1", "r", "c", owner_approved=True)


def test_full_big_change_flow(client, mock_api):
    order_route = mock_api.post("/v2/checkout/orders").mock(return_value=httpx.Response(200, json={
        "id": "SUP1", "status": "PAYER_ACTION_REQUIRED",
        "links": [{"rel": "payer-action", "href": "https://approve/SUP1"}]}))
    sup_cap = mock_api.post("/v2/checkout/orders/SUP1/capture").mock(return_value=httpx.Response(201, json={
        "purchase_units": [{"payments": {"captures": [{"id": "C2", "status": "COMPLETED",
                                                      "amount": {"currency_code": "USD", "value": "51.00"}}]}}]}))
    hold_cap = mock_api.post(AUTH_CAPTURE).mock(side_effect=capture_reply)

    items = [VISIT, WASHER, CART]
    d = decide(HOLD, items, M, now=T0)
    order = request_price_change(client, d, items, "job-1|m:abc", "r", "c", owner_approved=True)
    pu = json.loads(order_route.calls.last.request.content)["purchase_units"][0]
    assert order.approve_url == "https://approve/SUP1"
    assert pu["amount"]["value"] == "51.00" and [i["name"] for i in pu["items"]] == ["Washer", "Faucet cartridge"]
    assert pu["custom_id"] == "job-1|m:abc|sup"

    s = complete_price_change(client, "AUTH1", d, "SUP1", "inv-1")
    assert sup_cap.called and json.loads(hold_cap.calls.last.request.content)["amount"]["value"] == "60.00"
    assert s.charged.amount == Decimal("111.00") and len(s.captures) == 2


def test_decline_charges_booked_amount(client, mock_api):
    route = mock_api.post(AUTH_CAPTURE).mock(side_effect=capture_reply)
    d = decide(HOLD, [VISIT, WASHER, CART], M, now=T0)
    s = decline_price_change(client, "AUTH1", d, "inv-1")
    assert json.loads(route.calls.last.request.content)["amount"]["value"] == "60.00"
    assert s.charged.amount == Decimal("60.00")


def test_supplemental_items_balance():
    # added items match the extra exactly
    assert [i.name for i in supplemental_items([VISIT, WASHER, CART], usd("51"))] == ["Washer", "Faucet cartridge"]
    # visit repriced up by $10 as well: a balancing line is added
    names = [i.name for i in supplemental_items([VISIT, WASHER], usd("16"))]
    assert names == ["Washer", "Other changes to the booked quote"]
    # nothing marked as added: one combined line
    one = supplemental_items([VISIT], usd("12"))
    assert len(one) == 1 and one[0].unit_amount.amount == Decimal("12.00")
