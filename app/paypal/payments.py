"""PayPal Payments v2: capture, void, reauthorize and read authorizations (the hold)."""
from __future__ import annotations

from app.paypal.client import PayPalClient
from app.paypal.models import Authorization, Capture, Money

# Ask PayPal for the full resource (amount, status details) instead of the minimal default.
FULL_REPRESENTATION = {"Prefer": "return=representation"}


def capture_authorization(
    client: PayPalClient,
    auth_id: str,
    amount: Money,
    final_capture: bool = True,
    note: str | None = None,
    invoice_id: str | None = None,
    request_id: str | None = None,
) -> Capture:
    """POST /v2/payments/authorizations/{id}/capture

    `amount` may be less than the hold (partial) or up to the over-capture cap.
    `final_capture=True` releases whatever is left of the hold."""
    body: dict = {"amount": amount.to_paypal(), "final_capture": final_capture}
    if note:
        body["note_to_payer"] = note[:255]
    if invoice_id:
        body["invoice_id"] = invoice_id[:127]
    data = client.request("POST", f"/v2/payments/authorizations/{auth_id}/capture", body, request_id,
                          extra_headers=FULL_REPRESENTATION)
    return Capture.from_paypal(data)


def void_authorization(client: PayPalClient, auth_id: str, request_id: str | None = None) -> None:
    """POST /v2/payments/authorizations/{id}/void - releases the whole hold (204)."""
    client.request("POST", f"/v2/payments/authorizations/{auth_id}/void", None, request_id)


def reauthorize(client: PayPalClient, auth_id: str, amount: Money, request_id: str | None = None) -> Authorization:
    """POST /v2/payments/authorizations/{id}/reauthorize - refresh a hold past its 3-day honor period."""
    data = client.request(
        "POST", f"/v2/payments/authorizations/{auth_id}/reauthorize", {"amount": amount.to_paypal()}, request_id
    )
    return Authorization.from_paypal(data)


def get_authorization(client: PayPalClient, auth_id: str) -> Authorization:
    """GET /v2/payments/authorizations/{id}"""
    return Authorization.from_paypal(client.request("GET", f"/v2/payments/authorizations/{auth_id}"))
