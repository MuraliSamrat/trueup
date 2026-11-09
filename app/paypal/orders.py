"""PayPal Orders v2: create hold (AUTHORIZE) and supplemental (CAPTURE) orders."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.paypal.client import PayPalClient
from app.paypal.models import Authorization, Capture, LineItem, Money, OrderResult


def _experience(return_url: str, cancel_url: str, brand_name: str) -> dict[str, Any]:
    return {
        "paypal": {
            "experience_context": {
                "brand_name": brand_name[:127],
                "user_action": "CONTINUE",
                "shipping_preference": "NO_SHIPPING",  # services: nothing is shipped
                "return_url": return_url,
                "cancel_url": cancel_url,
            }
        }
    }


def create_authorize_order(
    client: PayPalClient,
    amount: Money,
    description: str,
    custom_id: str,
    return_url: str,
    cancel_url: str,
    brand_name: str = "TrueUp",
    request_id: str | None = None,
) -> OrderResult:
    """POST /v2/checkout/orders with intent=AUTHORIZE. Creates the booking hold."""
    body = {
        "intent": "AUTHORIZE",
        "purchase_units": [
            {
                "reference_id": custom_id[:256],
                "custom_id": custom_id[:127],
                "description": description[:127],
                "amount": amount.to_paypal(),
            }
        ],
        "payment_source": _experience(return_url, cancel_url, brand_name),
    }
    return OrderResult.from_paypal(client.request("POST", "/v2/checkout/orders", body, request_id))


def create_supplemental_order(
    client: PayPalClient,
    items: list[LineItem],
    description: str,
    custom_id: str,
    return_url: str,
    cancel_url: str,
    brand_name: str = "TrueUp",
    request_id: str | None = None,
) -> OrderResult:
    """POST /v2/checkout/orders with intent=CAPTURE, itemized.

    Used when the new total is above the over-capture cap: the customer approves
    the extra amount as its own order, with every added item listed."""
    if not items:
        raise ValueError("A supplemental order needs at least one item")
    currency = items[0].unit_amount.currency
    total_money = Money(amount=sum((i.total.amount for i in items), Decimal("0")), currency=currency)
    body = {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "reference_id": custom_id[:256],
                "custom_id": custom_id[:127],
                "description": description[:127],
                "amount": {
                    **total_money.to_paypal(),
                    "breakdown": {"item_total": total_money.to_paypal()},
                },
                "items": [i.to_paypal() for i in items],
            }
        ],
        "payment_source": _experience(return_url, cancel_url, brand_name),
    }
    return OrderResult.from_paypal(client.request("POST", "/v2/checkout/orders", body, request_id))


def get_order(client: PayPalClient, order_id: str) -> dict[str, Any]:
    """GET /v2/checkout/orders/{id}"""
    return client.request("GET", f"/v2/checkout/orders/{order_id}")


def authorize_order(client: PayPalClient, order_id: str, request_id: str | None = None) -> Authorization:
    """POST /v2/checkout/orders/{id}/authorize - after the buyer approved. Returns the hold."""
    data = client.request("POST", f"/v2/checkout/orders/{order_id}/authorize", None, request_id)
    unit = data["purchase_units"][0]
    auth = unit["payments"]["authorizations"][0]
    auth.setdefault("custom_id", unit.get("custom_id"))
    return Authorization.from_paypal(auth)


def capture_order(client: PayPalClient, order_id: str, request_id: str | None = None) -> Capture:
    """POST /v2/checkout/orders/{id}/capture - for CAPTURE-intent (supplemental) orders."""
    data = client.request("POST", f"/v2/checkout/orders/{order_id}/capture", None, request_id,
                          extra_headers={"Prefer": "return=representation"})
    return Capture.from_paypal(data["purchase_units"][0]["payments"]["captures"][0])
