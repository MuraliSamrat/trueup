"""PayPal webhooks: verify the signature with PayPal, then let the service react.

POST /v1/notifications/verify-webhook-signature
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from app.paypal.client import PayPalClient

SIGNATURE_HEADERS = {
    "auth_algo": "paypal-auth-algo",
    "cert_url": "paypal-cert-url",
    "transmission_id": "paypal-transmission-id",
    "transmission_sig": "paypal-transmission-sig",
    "transmission_time": "paypal-transmission-time",
}


def verify_webhook(client: PayPalClient, webhook_id: str, headers: Mapping[str, str], raw_body: bytes) -> bool:
    """Ask PayPal whether this webhook delivery is genuine. Returns False on any missing header."""
    lower = {k.lower(): v for k, v in headers.items()}
    fields = {k: lower.get(h) for k, h in SIGNATURE_HEADERS.items()}
    if not all(fields.values()) or not webhook_id:
        return False
    body = {**fields, "webhook_id": webhook_id, "webhook_event": json.loads(raw_body)}
    result = client.request("POST", "/v1/notifications/verify-webhook-signature", body)
    return result.get("verification_status") == "SUCCESS"


def event_order_id(event: dict[str, Any]) -> str | None:
    """Order id for CHECKOUT.ORDER.* events, or from a capture/authorization's related ids."""
    res = event.get("resource") or {}
    if event.get("event_type", "").startswith("CHECKOUT.ORDER"):
        return res.get("id")
    return ((res.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id")
