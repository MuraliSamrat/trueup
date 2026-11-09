"""Bill agent: technician's voice note (transcript) + photo -> priced line items.

    agent (LangChain create_agent, structured DraftBill)
      tools:       search_price_list, get_price_item
      middleware:  PII redaction -> model retry -> model fallback -> call limits
                   -> price guard (custom, after_model) -> audit log (custom, wrap_*)
    then code (not AI) turns SKUs into priced LineItems and runs the decision engine.
"""
from __future__ import annotations

import base64
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ModelFallbackMiddleware,
    ModelRetryMiddleware,
    ToolCallLimitMiddleware,
)
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import tool
from pydantic import BaseModel

from app.agents.middleware import AuditLogMiddleware, PriceGuardMiddleware, pii_middlewares
from app.agents.price_list import PriceList
from app.agents.schemas import DraftBill
from app.paypal.models import LineItem

SYSTEM_PROMPT = """You are TrueUp's billing assistant for {business}, a home-services company.
A technician has finished (or is finishing) a booked job. From the technician's voice note
and photo, list every part and service actually used ON SITE, as items from the price list.

Rules:
- ALWAYS call search_price_list to find items. Use only SKUs it returns. Never invent SKUs.
- Never state or guess prices. You choose SKUs and quantities; the system prices them.
- Do NOT include the booked visit ({booked_sku}); it is already on the bill.
- Only include work the technician says was done (or clearly must be done now). If they only
  mention something might be needed later, put it in open_questions, not in lines.
- Each line needs a short reason quoting the technician's note or what the photo shows.
- If the photo does not support an added item, set photo_supports_change accordingly and say so in open_questions.
- Write the summary for the customer: plain language, no jargon, no prices.
"""


def build_tools(price_list: PriceList) -> list:
    @tool
    def search_price_list(query: str) -> str:
        """Search the business price list for parts or services matching a description,
        e.g. 'worn washer under sink'. Returns up to 5 lines: SKU | name | category | price."""
        hits = price_list.search(query, k=5)
        return "\n".join(h.brief() for h in hits) or "No matching items. Try different words."

    @tool
    def get_price_item(sku: str) -> str:
        """Look up one price-list item by its exact SKU."""
        item = price_list.get(sku)
        return item.brief() if item else f"Unknown SKU {sku}"

    return [search_price_list, get_price_item]


def build_bill_agent(
    model: BaseChatModel | str,
    price_list: PriceList,
    audit: AuditLogMiddleware | None = None,
    fallback_model: BaseChatModel | str | None = None,
    booked_sku: str = "VIS-STD",
):
    middleware: list[Any] = [*pii_middlewares(), ModelRetryMiddleware(max_retries=2, initial_delay=0.5)]
    if fallback_model is not None:
        middleware.append(ModelFallbackMiddleware(fallback_model))
    middleware += [
        ModelCallLimitMiddleware(run_limit=8, exit_behavior="end"),
        ToolCallLimitMiddleware(run_limit=12),
        PriceGuardMiddleware(price_list),
        audit or AuditLogMiddleware(),
    ]
    return create_agent(
        model,
        tools=build_tools(price_list),
        system_prompt=SYSTEM_PROMPT.format(business=price_list.business, booked_sku=booked_sku),
        response_format=ToolStrategy(DraftBill),
        middleware=middleware,
        name="bill_agent",
    )


def visit_message(transcript: str, photo: bytes | None = None, photo_mime: str = "image/jpeg",
                  job_context: str | None = None) -> dict[str, Any]:
    text = (f"Booked job: {job_context}\n\n" if job_context else "") + f"Technician's voice note (transcript):\n{transcript}"
    if not photo:
        return {"role": "user", "content": text}
    b64 = base64.b64encode(photo).decode("ascii")
    return {"role": "user", "content": [
        {"type": "text", "text": text + "\n\nPhoto from the job is attached."},
        {"type": "image_url", "image_url": {"url": f"data:{photo_mime};base64,{b64}"}},
    ]}


class BillResult(BaseModel):
    draft: DraftBill | None
    added_items: list[LineItem]          # priced by code from the price list
    dropped: list[str]                   # lines the guard could not fix (flagged for the owner)
    audit: list[dict[str, Any]]


def price_draft(draft: DraftBill, price_list: PriceList) -> tuple[list[LineItem], list[str]]:
    """Deterministic pricing. Unknown SKUs are dropped and reported, never priced."""
    items, dropped = [], []
    for line in draft.lines:
        if price_list.get(line.sku) is None or line.sku.strip().upper() == "VIS-STD":
            dropped.append(f"{line.sku} x{line.quantity}: {line.reason}")
            continue
        items.append(price_list.line_item(line.sku, line.quantity, added=True, reason=line.reason))
    return items, dropped


def draft_bill(agent, price_list: PriceList, audit: AuditLogMiddleware, transcript: str,
               photo: bytes | None = None, photo_mime: str = "image/jpeg", job_context: str | None = None) -> BillResult:
    out = agent.invoke({"messages": [visit_message(transcript, photo, photo_mime, job_context)]})
    draft = out.get("structured_response")
    if not isinstance(draft, DraftBill):
        return BillResult(draft=None, added_items=[], dropped=["The agent did not produce a bill; owner must enter it."],
                          audit=audit.entries)
    items, dropped = price_draft(draft, price_list)
    return BillResult(draft=draft, added_items=items, dropped=dropped, audit=audit.entries)
