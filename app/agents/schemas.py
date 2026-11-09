"""Structured output the bill agent must return. Note: no prices here - the AI picks
SKUs and quantities only; code attaches prices from the price list."""
from __future__ import annotations

from pydantic import BaseModel, Field


class DraftLine(BaseModel):
    sku: str = Field(description="SKU from the price list, exactly as returned by search_price_list")
    quantity: int = Field(default=1, ge=1, le=20, description="How many units")
    reason: str = Field(description="Short evidence from the technician's note or photo, e.g. 'washer worn, sink dripping'")


class DraftBill(BaseModel):
    """The work actually done on site, as price-list items."""

    lines: list[DraftLine] = Field(description="Every part or service used on site, EXCLUDING the booked visit itself")
    summary: str = Field(description="One or two plain sentences for the customer explaining what changed and why")
    photo_supports_change: bool | None = Field(
        default=None, description="Does the photo show evidence for the added work? null if no photo")
    open_questions: list[str] = Field(
        default_factory=list, description="Anything unclear the owner should check before approving")
