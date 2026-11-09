"""Custom middleware for TrueUp agents.

PriceGuardMiddleware  (after_model)  - every SKU the AI returns must exist in the price list
                                       and quantities must be sane; otherwise send the model back
                                       with the problem (bounded retries).
AuditLogMiddleware    (wrap_tool_call + wrap_model_call) - records every tool call and model
                                       call (name, args, timing, status) as evidence for the receipt.
pii_middlewares()     - prebuilt PIIMiddleware for emails, phone numbers and street addresses,
                        so customer contact details never reach the model.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware, PIIMatch, PIIMiddleware, hook_config
from langchain_core.messages import HumanMessage

from app.agents.price_list import PriceList
from app.agents.schemas import DraftBill

GUARD_NAME = "price_guard"


class PriceGuardMiddleware(AgentMiddleware):
    """Reject drafts that reference unknown SKUs or absurd quantities."""

    def __init__(self, price_list: PriceList, max_retries: int = 2, max_quantity: int = 10) -> None:
        super().__init__()
        self.price_list = price_list
        self.max_retries = max_retries
        self.max_quantity = max_quantity

    def problems(self, draft: DraftBill) -> list[str]:
        out = []
        for line in draft.lines:
            if self.price_list.get(line.sku) is None:
                out.append(f"SKU '{line.sku}' is not in the price list. Use search_price_list and pick a real SKU.")
            if line.quantity > self.max_quantity:
                out.append(f"Quantity {line.quantity} for {line.sku} is too high (max {self.max_quantity}).")
            if line.sku.strip().upper().startswith("VIS-STD"):
                out.append("Do not include the booked visit (VIS-STD); it is already on the bill.")
        return out

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        draft = state.get("structured_response")
        if not isinstance(draft, DraftBill):
            return None
        problems = self.problems(draft)
        if not problems:
            return None
        retries = sum(1 for m in state["messages"] if isinstance(m, HumanMessage) and m.name == GUARD_NAME)
        if retries >= self.max_retries:
            return None  # give up; the pipeline drops bad lines and flags them for the owner
        msg = "Your bill was rejected by the price guard:\n- " + "\n- ".join(problems) + "\nReturn a corrected bill."
        return {"messages": [HumanMessage(content=msg, name=GUARD_NAME)], "jump_to": "model"}


class AuditLogMiddleware(AgentMiddleware):
    """Append-only record of what the agent did. `entries` is shared with the caller."""

    def __init__(self, entries: list[dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.entries: list[dict[str, Any]] = entries if entries is not None else []

    def _log(self, **kw: Any) -> None:
        self.entries.append({"at": datetime.now(timezone.utc).isoformat(), **kw})

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        call = request.tool_call
        start = time.perf_counter()
        try:
            result = handler(request)
            content = getattr(result, "content", "")
            self._log(kind="tool", name=call["name"], args=call.get("args", {}), ok=True,
                      ms=round((time.perf_counter() - start) * 1000, 1),
                      result_preview=(content if isinstance(content, str) else str(content))[:200])
            return result
        except Exception as e:
            self._log(kind="tool", name=call["name"], args=call.get("args", {}), ok=False, error=repr(e)[:200],
                      ms=round((time.perf_counter() - start) * 1000, 1))
            raise

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        start = time.perf_counter()
        response = handler(request)
        model = getattr(request.model, "model_name", None) or getattr(request.model, "model", None) or type(request.model).__name__
        usage = None
        try:
            msg = response.result[-1]
            usage = getattr(msg, "usage_metadata", None)
        except Exception:
            pass
        self._log(kind="model", model=str(model), ms=round((time.perf_counter() - start) * 1000, 1), usage=usage)
        return response


# ---- PII ------------------------------------------------------------------
_PHONE = re.compile(r"(?<!\w)(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\w)")
_ADDRESS = re.compile(
    r"\b\d{1,6}\s+(?:[A-Z][a-z]+\s){1,4}(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Lane|Ln|Dr|Drive|Way|Ct|Court)\b\.?")


def _regex_detector(pattern: re.Pattern[str], pii_type: str) -> Callable[[str], list[PIIMatch]]:
    def detect(text: str) -> list[PIIMatch]:
        return [PIIMatch(type=pii_type, value=m.group(0), start=m.start(), end=m.end()) for m in pattern.finditer(text)]
    return detect


def pii_middlewares() -> list[PIIMiddleware]:
    return [
        PIIMiddleware("email", strategy="redact", apply_to_input=True),
        PIIMiddleware("phone_number", strategy="mask", detector=_regex_detector(_PHONE, "phone_number"), apply_to_input=True),
        PIIMiddleware("street_address", strategy="redact", detector=_regex_detector(_ADDRESS, "street_address"), apply_to_input=True),
    ]
