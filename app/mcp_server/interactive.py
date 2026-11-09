"""Elicitation, sampling and progress for TrueUp tools, on both MCP protocol eras.

MCP 2026-07-28 (SEP-2322) removed server-initiated requests: a tool that needs the user's input
(elicitation) or the client's LLM (sampling) RETURNS an `InputRequiredResult`; the client fulfils
the embedded requests and calls the tool again with `input_responses`. Older clients (2025-11-25)
still get classic server-initiated requests. This module hides the difference:

    ui = Interaction(ctx)
    choice = await ui.choose("rule", "How should extras be handled?", [...])
    text = await ui.sample("explain", prompt, system=...)
    if ui.needs_input:              # modern era, first pass: ask and come back
        return ui.input_required()
    ...                             # second pass (or legacy): answers are available

Every feature degrades gracefully: if the client did not declare the capability, choose()/sample()
return None and the tool falls back (default rule, business-written summary, link in the result).
Note: sampling is deprecated in the 2026-07-28 spec (SEP-2577) but still part of the wire format;
TrueUp uses it only as an optional enhancement.
"""
from __future__ import annotations

import warnings
from typing import Any

import mcp_types as mt
from fastmcp import Context

DECLINED = "__declined__"


class Interaction:
    def __init__(self, ctx: Context | None) -> None:
        self.ctx = ctx
        self._pending: dict[str, Any] = {}
        self.responses: dict[str, Any] = {}
        self.modern = False
        self.can_elicit = self.can_sample = False
        if ctx is None:
            return
        try:
            self.modern = bool(ctx._is_modern_protocol())
            self.responses = dict(ctx.input_responses or {})
            session = ctx.session
            self.can_elicit = session.check_client_capability(
                mt.ClientCapabilities(elicitation=mt.ElicitationCapability()))
            self.can_sample = session.check_client_capability(
                mt.ClientCapabilities(sampling=mt.SamplingCapability()))
        except Exception:  # no session (direct call / tests): no interaction
            pass

    # ---- elicitation ----------------------------------------------------------------
    async def choose(self, key: str, message: str, choices: list[str]) -> str | None:
        """Ask the user to pick one option. Returns the choice, DECLINED, or None if the client
        can't ask (or, in the modern era, the question is pending - check needs_input)."""
        if not self.can_elicit:
            return None
        if self.modern:
            if key in self.responses:
                r = self.responses[key]
                if getattr(r, "action", None) != "accept":
                    return DECLINED
                return (r.content or {}).get("choice")
            self._pending[key] = mt.ElicitRequest(params=mt.ElicitRequestFormParams(
                message=message,
                requested_schema={"type": "object", "required": ["choice"],
                                  "properties": {"choice": {"type": "string", "title": "Your choice", "enum": choices}}}))
            return None
        try:
            r = await self.ctx.elicit(message, choices)
        except Exception:
            return None
        return r.data if r.action == "accept" else DECLINED

    # ---- sampling (deprecated in 2026-07-28, optional here) ----------------------------
    async def sample(self, key: str, prompt: str, system: str | None = None, max_tokens: int = 200) -> str | None:
        """Ask the CLIENT's model to write text (uses the user's own assistant, not our API key)."""
        if not self.can_sample:
            return None
        msg = [mt.SamplingMessage(role="user", content=mt.TextContent(type="text", text=prompt))]
        if self.modern:
            if key in self.responses:
                return _text(self.responses[key])
            self._pending[key] = mt.CreateMessageRequest(params=mt.CreateMessageRequestParams(
                messages=msg, system_prompt=system, max_tokens=max_tokens, temperature=0.2))
            return None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = await self.ctx.session.create_message(messages=msg, system_prompt=system, max_tokens=max_tokens,
                                                          temperature=0.2)
            return _text(r)
        except Exception:
            return None

    # ---- progress ----------------------------------------------------------------------
    async def progress(self, done: float, total: float, message: str) -> None:
        """Progress notification; a no-op if the client didn't send a progress token."""
        if self.ctx is None:
            return
        try:
            await self.ctx.report_progress(done, total, message)
        except Exception:
            pass

    # ---- modern-era round trip ---------------------------------------------------------------
    @property
    def needs_input(self) -> bool:
        return bool(self._pending)

    def input_required(self) -> mt.InputRequiredResult:
        return mt.InputRequiredResult(input_requests=dict(self._pending))


def _text(result: Any) -> str | None:
    content = getattr(result, "content", None)
    if isinstance(content, list):
        content = next((c for c in content if getattr(c, "type", "") == "text"), None)
    return getattr(content, "text", None)
