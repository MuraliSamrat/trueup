"""The customer's spending rule ("mandate") for price changes on a booked service.

The customer's AI agent records it at booking time, e.g.
  "auto-approve increases up to 10% or $20, always ask for emergency surcharges".
It is serialised to canonical JSON and hashed (SHA-256). The short hash travels in the
PayPal `custom_id`, so every hold and capture is tied to exactly the rule the customer
agreed to - and the receipt can prove it.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from app.paypal.models import Money, cap_for

SHORT_HASH_LEN = 16


class Mandate(BaseModel):
    mandate_id: str
    customer_ref: str                       # opaque id for the customer/agent, never PII
    currency: str = "USD"
    # Auto-approve limits for increases above the hold. None = no extra limit of our own;
    # PayPal's over-capture cap (min(115%, +$75)) always applies on top.
    max_auto_increase_pct: Decimal | None = Field(default=None, ge=0)
    max_auto_increase_amount: Decimal | None = Field(default=None, ge=0)
    always_ask_categories: list[str] = Field(default_factory=list)
    created_at: datetime
    expires_at: datetime | None = None
    note: str | None = None                 # the customer's words, e.g. "small extras are fine"

    @field_validator("max_auto_increase_pct", "max_auto_increase_amount", mode="before")
    @classmethod
    def _no_float(cls, v):
        if isinstance(v, float):
            raise TypeError("Use str/int/Decimal for mandate limits, not float")
        return v

    @field_validator("always_ask_categories")
    @classmethod
    def _normalise(cls, v: list[str]) -> list[str]:
        return sorted({c.strip().lower() for c in v if c.strip()})

    # ---- integrity -------------------------------------------------------
    def canonical_json(self) -> str:
        """Stable serialisation: sorted keys, no whitespace, decimals/dates as strings."""
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @property
    def short_hash(self) -> str:
        return self.sha256[:SHORT_HASH_LEN]

    def verify(self, expected_sha256: str) -> bool:
        return self.sha256 == expected_sha256

    # ---- rules -------------------------------------------------------------
    def is_expired(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return self.expires_at is not None and now >= self.expires_at

    def auto_limit(self, hold: Money) -> Money:
        """Highest total that may be charged without asking anyone:
        the tightest of PayPal's cap and the customer's own % / $ limits."""
        if hold.currency != self.currency:
            raise ValueError(f"Mandate currency {self.currency} != hold currency {hold.currency}")
        limits = [cap_for(hold).amount]
        if self.max_auto_increase_pct is not None:
            limits.append(hold.amount * (1 + self.max_auto_increase_pct / 100))
        if self.max_auto_increase_amount is not None:
            limits.append(hold.amount + self.max_auto_increase_amount)
        return Money(amount=min(limits).quantize(Decimal("0.01"), rounding="ROUND_DOWN"), currency=hold.currency)


def custom_id_for(job_id: str, mandate: Mandate) -> str:
    """Value for PayPal purchase_unit.custom_id (max 127 chars): job id + mandate short hash."""
    return f"{job_id}|m:{mandate.short_hash}"[:127]


def parse_custom_id(custom_id: str) -> tuple[str, str | None]:
    job_id, _, rest = custom_id.partition("|m:")
    return job_id, (rest or None)
