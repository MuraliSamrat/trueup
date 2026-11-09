"""Typed models for PayPal money, orders, authorizations and captures.

Money is always Decimal (never float) and is sent to PayPal as a 2-dp string.
"""
from __future__ import annotations

from datetime import datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any

from pydantic import BaseModel, field_validator

CENT = Decimal("0.01")

# PayPal's default over-capture limit: total captures may reach 115% of the
# authorized amount, but never more than authorized + 75.00 USD.
OVERCAPTURE_PCT = Decimal("1.15")
OVERCAPTURE_MAX_EXTRA = Decimal("75.00")


class Money(BaseModel):
    amount: Decimal
    currency: str = "USD"

    @field_validator("amount", mode="before")
    @classmethod
    def _to_decimal(cls, v: Any) -> Decimal:
        if isinstance(v, float):
            raise TypeError("Money amount must not be a float; use str, int or Decimal")
        return Decimal(str(v)).quantize(CENT, rounding=ROUND_HALF_UP)

    def to_paypal(self) -> dict[str, str]:
        return {"currency_code": self.currency, "value": f"{self.amount:.2f}"}

    @classmethod
    def from_paypal(cls, data: dict[str, str]) -> Money:
        return cls(amount=data["value"], currency=data["currency_code"])

    def __add__(self, other: Money) -> Money:
        self._same_currency(other)
        return Money(amount=self.amount + other.amount, currency=self.currency)

    def __sub__(self, other: Money) -> Money:
        self._same_currency(other)
        return Money(amount=self.amount - other.amount, currency=self.currency)

    def _same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise ValueError(f"Currency mismatch: {self.currency} vs {other.currency}")

    def __str__(self) -> str:
        return f"{self.amount:.2f} {self.currency}"


def usd(amount: str | int | Decimal) -> Money:
    return Money(amount=amount, currency="USD")


def cap_for(hold: Money) -> Money:
    """Largest total that can be captured on an authorization of `hold` without
    new buyer consent: min(hold x 1.15, hold + 75.00), rounded DOWN to the cent."""
    pct = (hold.amount * OVERCAPTURE_PCT).quantize(CENT, rounding=ROUND_DOWN)
    flat = hold.amount + OVERCAPTURE_MAX_EXTRA
    return Money(amount=min(pct, flat), currency=hold.currency)


class LineItem(BaseModel):
    name: str
    unit_amount: Money
    quantity: int = 1
    description: str | None = None
    # TrueUp-only fields (never sent to PayPal):
    category: str | None = None   # e.g. "visit", "parts", "labor", "emergency"
    added: bool = False           # True if added on site (not part of the booked quote)

    @property
    def total(self) -> Money:
        return Money(amount=self.unit_amount.amount * self.quantity, currency=self.unit_amount.currency)

    def to_paypal(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            "name": self.name[:127],
            "unit_amount": self.unit_amount.to_paypal(),
            "quantity": str(self.quantity),
        }
        if self.description:
            item["description"] = self.description[:127]
        return item


class OrderResult(BaseModel):
    id: str
    status: str
    approve_url: str | None = None

    @classmethod
    def from_paypal(cls, data: dict[str, Any]) -> OrderResult:
        approve = next(
            (l["href"] for l in data.get("links", []) if l.get("rel") in ("payer-action", "approve")),
            None,
        )
        return cls(id=data["id"], status=data["status"], approve_url=approve)


class Authorization(BaseModel):
    id: str
    status: str
    amount: Money | None = None
    expiration_time: datetime | None = None
    custom_id: str | None = None

    @classmethod
    def from_paypal(cls, data: dict[str, Any]) -> Authorization:
        return cls(
            id=data["id"],
            status=data["status"],
            amount=Money.from_paypal(data["amount"]) if data.get("amount") else None,
            expiration_time=data.get("expiration_time"),
            custom_id=data.get("custom_id"),
        )


class Capture(BaseModel):
    id: str
    status: str
    amount: Money | None = None
    final_capture: bool | None = None

    @classmethod
    def from_paypal(cls, data: dict[str, Any]) -> Capture:
        return cls(
            id=data["id"],
            status=data["status"],
            amount=Money.from_paypal(data["amount"]) if data.get("amount") else None,
            final_capture=data.get("final_capture"),
        )
