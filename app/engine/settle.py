"""Carry out a Decision with PayPal. This is the only place the engine touches money.

    VOID            -> void the authorization
    CAPTURE_WITHIN  -> capture the real total on the hold (rest is released)
    AUTO_CAPTURE    -> capture the increased total on the hold (PayPal over-capture)
    NEEDS_APPROVAL  -> two calls, because a human is involved:
        request_price_change()  - after the OWNER approved: create an itemized supplemental
                                  order and return the link for the customer's agent
        complete_price_change() - after the CUSTOMER approved: capture the supplemental order,
                                  then capture the original hold
        decline_price_change()  - customer said no: charge only the booked amount on the hold
"""
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel

from app.engine.decide import Action, Decision
from app.paypal.client import PayPalClient
from app.paypal.models import Capture, LineItem, Money, OrderResult
from app.paypal.orders import capture_order, create_supplemental_order
from app.paypal.payments import capture_authorization, void_authorization


class Settlement(BaseModel):
    action: Action
    captures: list[Capture] = []
    voided: bool = False
    charged: Money
    summary: str


class ApprovalError(Exception):
    """Raised when a price change is attempted without the required approval."""


def settle(client: PayPalClient, auth_id: str, decision: Decision, invoice_id: str, note: str | None = None) -> Settlement:
    """Settle VOID / CAPTURE_WITHIN / AUTO_CAPTURE directly. NEEDS_APPROVAL must use the
    request/complete functions below."""
    zero = Money(amount=0, currency=decision.hold.currency)
    if decision.action == Action.VOID:
        void_authorization(client, auth_id, request_id=f"{invoice_id}:void")
        return Settlement(action=decision.action, voided=True, charged=zero,
                          summary=f"Hold of {decision.hold} released; customer charged {zero}.")
    if decision.action in (Action.CAPTURE_WITHIN, Action.AUTO_CAPTURE):
        cap = capture_authorization(client, auth_id, decision.capture_on_hold, final_capture=True,
                                    note=note, invoice_id=invoice_id, request_id=f"{invoice_id}:hold-capture")
        return Settlement(action=decision.action, captures=[cap], charged=decision.capture_on_hold,
                          summary=f"Charged {decision.capture_on_hold} on the {decision.hold} hold.")
    raise ApprovalError("This decision needs owner + customer approval; use request_price_change().")


def supplemental_items(items: list[LineItem], extra: Money) -> list[LineItem]:
    """Items for the supplemental order: the ones added on site. If they don't add up to
    exactly `extra` (e.g. the booked visit was repriced), add one balancing line so the
    order total always equals the amount the customer is asked to approve."""
    added = [i for i in items if i.added]
    added_total = sum((i.total.amount for i in added), Decimal("0"))
    diff = extra.amount - added_total
    if diff > 0:
        added.append(LineItem(name="Other changes to the booked quote", unit_amount=Money(amount=diff, currency=extra.currency)))
    elif diff < 0 or not added:
        return [LineItem(name="Price change", unit_amount=extra,
                         description=", ".join(i.name for i in items if i.added)[:127] or None)]
    return added


def request_price_change(
    client: PayPalClient,
    decision: Decision,
    items: list[LineItem],
    custom_id: str,
    return_url: str,
    cancel_url: str,
    owner_approved: bool,
    description: str | None = None,
) -> OrderResult:
    if decision.action != Action.NEEDS_APPROVAL:
        raise ApprovalError(f"Decision is {decision.action}, no price-change approval is needed.")
    if not owner_approved:
        raise ApprovalError("The owner must approve the price change before the customer is asked.")
    sup_items = supplemental_items(items, decision.supplemental)
    desc = description or f"Price change: +{decision.supplemental} ({', '.join(i.name for i in sup_items)})"
    return create_supplemental_order(client, sup_items, desc, f"{custom_id}|sup"[:127], return_url, cancel_url,
                                     request_id=f"{custom_id.split('|')[0]}:extra-order")


def complete_price_change(client: PayPalClient, auth_id: str, decision: Decision, supplemental_order_id: str,
                          invoice_id: str, note: str | None = None) -> Settlement:
    """Customer approved the supplemental order: capture it, then capture the booked hold."""
    sup = capture_order(client, supplemental_order_id, request_id=f"{invoice_id}:extra-capture")
    hold_cap = capture_authorization(client, auth_id, decision.capture_on_hold, final_capture=True,
                                     note=note, invoice_id=invoice_id, request_id=f"{invoice_id}:hold-capture")
    return Settlement(action=decision.action, captures=[sup, hold_cap], charged=decision.total,
                      summary=f"Charged {decision.capture_on_hold} on the hold + {decision.supplemental} "
                              f"approved price change = {decision.total}.")


def decline_price_change(client: PayPalClient, auth_id: str, decision: Decision, invoice_id: str) -> Settlement:
    """Customer declined the change: charge only what was booked."""
    cap = capture_authorization(client, auth_id, decision.hold, final_capture=True,
                                note="Price change declined; booked amount only", invoice_id=invoice_id,
                                request_id=f"{invoice_id}:hold-capture")
    return Settlement(action=decision.action, captures=[cap], charged=decision.hold,
                      summary=f"Price change declined; charged the booked {decision.hold}.")
