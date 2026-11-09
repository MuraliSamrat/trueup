"""TrueUp decision engine: given the hold, the real bill and the customer's spending rule,
decide how to settle. Pure function, no I/O, no AI - money decisions must be predictable.

    cancelled / $0 bill          -> VOID              release the hold
    total <= hold                -> CAPTURE_WITHIN    charge the real (lower or equal) total
    hold < total <= auto limit   -> AUTO_CAPTURE      charge the increase without asking
      (and no always-ask item, mandate not expired)
    otherwise                    -> NEEDS_APPROVAL    owner approves, then the customer's agent
                                                      approves a supplemental order for the rest
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel

from app.engine.mandate import Mandate
from app.paypal.models import LineItem, Money, cap_for


class Action(str, Enum):
    VOID = "VOID"
    CAPTURE_WITHIN = "CAPTURE_WITHIN"
    AUTO_CAPTURE = "AUTO_CAPTURE"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"


class Decision(BaseModel):
    action: Action
    hold: Money
    total: Money
    paypal_cap: Money
    auto_limit: Money
    capture_on_hold: Money          # amount to capture on the original authorization
    supplemental: Money             # amount the customer must approve as a new order (0 if none)
    needs_owner: bool
    needs_customer: bool
    reasons: list[str]
    mandate_sha256: str

    @property
    def increase(self) -> Money:
        return Money(amount=max(self.total.amount - self.hold.amount, Decimal("0")), currency=self.total.currency)


def bill_total(items: list[LineItem], currency: str) -> Money:
    for i in items:
        if i.unit_amount.currency != currency:
            raise ValueError(f"Item '{i.name}' is in {i.unit_amount.currency}, expected {currency}")
        if i.unit_amount.amount < 0 or i.quantity < 1:
            raise ValueError(f"Item '{i.name}' has a negative price or zero quantity")
    return Money(amount=sum((i.total.amount for i in items), Decimal("0")), currency=currency)


def decide(
    hold: Money,
    items: list[LineItem],
    mandate: Mandate,
    cancelled: bool = False,
    now: datetime | None = None,
) -> Decision:
    total = bill_total(items, hold.currency)
    zero = Money(amount=0, currency=hold.currency)
    cap = cap_for(hold)
    limit = mandate.auto_limit(hold)
    base = dict(hold=hold, total=total, paypal_cap=cap, auto_limit=limit, mandate_sha256=mandate.sha256)

    # 1) Nothing to charge.
    if cancelled or total.amount == 0:
        why = "Job cancelled" if cancelled else "Final bill is $0"
        base["total"] = zero
        return Decision(action=Action.VOID, capture_on_hold=zero, supplemental=zero, needs_owner=False,
                        needs_customer=False, reasons=[f"{why}: release the whole hold."], **base)

    # 2) Real bill is at or below what the customer already approved.
    if total.amount <= hold.amount:
        released = hold - total
        return Decision(action=Action.CAPTURE_WITHIN, capture_on_hold=total, supplemental=zero,
                        needs_owner=False, needs_customer=False,
                        reasons=[f"Bill {total} is within the {hold} hold; charge {total}"
                                 + (f" and release {released}." if released.amount else ".")], **base)

    # 3) Increase. Can it be charged automatically?
    reasons: list[str] = []
    flagged = sorted({(i.category or "").lower() for i in items}
                     & set(mandate.always_ask_categories))
    if mandate.is_expired(now):
        reasons.append("Customer's spending rule has expired, so any increase needs fresh consent.")
    if flagged:
        reasons.append(f"Bill contains always-ask item(s): {', '.join(flagged)}.")
    if total.amount > limit.amount:
        if total.amount > cap.amount:
            reasons.append(f"Total {total} is above PayPal's over-capture cap {cap} (min of 115% and +$75).")
        else:
            reasons.append(f"Total {total} is above the customer's auto-approve limit {limit}.")

    if not reasons:
        return Decision(action=Action.AUTO_CAPTURE, capture_on_hold=total, supplemental=zero,
                        needs_owner=False, needs_customer=False,
                        reasons=[f"Increase of {total - hold} is within the auto-approve limit {limit} "
                                 "and allowed by the customer's rule; charge on the original hold."], **base)

    # 4) Needs owner, then customer: keep the hold for the booked amount, ask for the rest.
    return Decision(action=Action.NEEDS_APPROVAL, capture_on_hold=hold, supplemental=total - hold,
                    needs_owner=True, needs_customer=True,
                    reasons=reasons + [f"Owner approves, then the customer's agent approves a supplemental "
                                       f"order for {total - hold}."], **base)
