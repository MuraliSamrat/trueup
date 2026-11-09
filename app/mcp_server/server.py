"""TrueUp MCP server: what a customer's AI assistant (e.g. Claude Desktop) uses to book a
local service, hold payment with PayPal, and approve or decline price changes on site.

MCP features used:
  tools (10) . resources (2) + resource template (1) . prompt (1) . server instructions
  tool annotations . structured results . clean tool errors . stdio + HTTP transports
  elicitation  - the user picks their spending rule, and confirms a price change, directly
  sampling     - the user's own assistant writes the plain-language explanation of a price change
  progress     - live updates while TrueUp waits for PayPal approval and captures payments
Elicitation/sampling work on both protocol eras (see interactive.py) and fall back gracefully.

Run (stdio, for Claude Desktop):   python app/mcp_server/server.py
Run (HTTP, for MCP Inspector):     python app/mcp_server/server.py --http   -> http://127.0.0.1:8001/mcp
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Annotated, Any, Callable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # find .env and data/ however Claude Desktop starts us

import mcp_types as mt  # noqa: E402
from fastmcp import Context, FastMCP  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402
from pydantic import Field  # noqa: E402

from app.mcp_server.interactive import DECLINED, Interaction  # noqa: E402
from app.paypal.client import PayPalError  # noqa: E402
from app.paypal.orders import get_order  # noqa: E402
from app.service import ServiceError, TrueUp  # noqa: E402
from app.store import JobStatus  # noqa: E402

INSTRUCTIONS = """TrueUp lets you book a local home-service visit (plumber, electrician, AC/heating) for the user
and pay safely with PayPal, even when the final price changes on site.

How it works:
1. list_services, then get_slots. Ask the user which slot they want.
2. book_visit. TrueUp asks the user directly how extra costs found on site should be handled (their spending
   rule). If your client can't show that question, agree the rule with the user yourself and pass it in.
   book_visit returns an approve_url: the USER must open it and approve the hold at PayPal.
3. Call wait_for_paypal_approval (or confirm_booking once the user says they approved).
4. Later, get_job_status. If a price change is waiting, call view_price_change and explain it. Then
   approve_price_change: TrueUp asks the user to confirm, and returns the PayPal link for the extra amount.
   Call wait_for_paypal_approval again; it finishes the payment once the user approves at PayPal.
   Never approve a price change without the user's explicit yes. If they say no: decline_price_change.
Money amounts come from the business's price list and PayPal; never invent or change them."""

RULE_CHOICES = {
    "Auto-approve small extras (up to PayPal's 15% limit)": {},
    "Auto-approve up to $10, ask me above that": {"auto_approve_up_to_usd": "10"},
    "Always ask me before any extra charge": {"auto_approve_up_to_usd": "0"},
}
APPROVE_CHOICE = "Approve - take me to PayPal"
DECLINE_CHOICE = "Decline - charge only the booked amount"


def create_server(get_app: Callable[[], TrueUp]) -> FastMCP:
    mcp = FastMCP("TrueUp", instructions=INSTRUCTIONS)

    def run(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except (ServiceError, KeyError) as e:
            raise ToolError(str(e).strip("'")) from e
        except PayPalError as e:
            raise ToolError(f"PayPal error {e.status} {e.issue}: {e.description} (debug_id {e.debug_id})") from e

    async def arun(fn: Callable[[], Any]) -> Any:
        """Run blocking service/PayPal calls off the event loop (keeps progress flowing)."""
        return await asyncio.to_thread(run, fn)

    # ---- tools: catalog -------------------------------------------------------------
    @mcp.tool(annotations={"readOnlyHint": True})
    def list_services() -> dict[str, Any]:
        """List the services this business offers, with the booked visit price."""
        app = get_app()
        return {"business": app.catalog.business, "area": app.catalog.area, "services": app.list_services()}

    @mcp.tool(annotations={"readOnlyHint": True})
    def get_slots(
        service_id: Annotated[str, Field(description="Service id from list_services, e.g. 'plumbing'")],
        day: Annotated[str | None, Field(description="Optional day YYYY-MM-DD")] = None,
    ) -> dict[str, Any]:
        """Free visit slots (business local time) for the next few days."""
        app = get_app()
        def slots() -> dict[str, Any]:
            out: dict[str, Any] = {"timezone": app.catalog.timezone, "slots": app.get_slots(service_id, day)}
            if app.demo_mode:
                out["asap_slot"] = out["slots"][0] if out["slots"] else None
                out["demo_note"] = ("PayPal sandbox demo: no real money moves and it can be booked from anywhere. "
                                    "asap_slot is an on-demand slot: the technician can come right away, so it is "
                                    "fine to book even if it starts in a few minutes.")
            return out
        return run(slots)

    # ---- tools: booking ---------------------------------------------------------------
    @mcp.tool
    async def book_visit(
        service_id: Annotated[str, Field(description="Service id from list_services")],
        slot: Annotated[str, Field(description="A free slot exactly as returned by get_slots")],
        problem: Annotated[str, Field(description="What's wrong, in the user's words (no phone numbers or addresses)")],
        auto_approve_up_to_pct: Annotated[str | None, Field(
            description="Optional: auto-approve price increases up to this % (stricter than PayPal's 15%)")] = None,
        auto_approve_up_to_usd: Annotated[str | None, Field(
            description="Optional: auto-approve increases up to this many dollars; '0' = always ask")] = None,
        always_ask_about: Annotated[list[str] | None, Field(
            description="Optional categories that always need the user's OK, e.g. ['emergency', 'labor']")] = None,
        rule_note: Annotated[str | None, Field(description="The user's spending rule in their own words")] = None,
        ctx: Context | None = None,
    ) -> dict[str, Any] | mt.InputRequiredResult:
        """Book a visit and create a PayPal hold for the visit price. If no spending rule is given, TrueUp
        asks the user to choose one. Returns approve_url, which the USER must open to approve the hold;
        then call wait_for_paypal_approval or confirm_booking."""
        app = get_app()
        ui = Interaction(ctx)
        svc = run(lambda: app.catalog.get(service_id))
        if slot not in run(lambda: app.get_slots(service_id)):
            raise ToolError(f"Slot '{slot}' is not available. Use get_slots to pick a free one.")

        rule: dict[str, Any] = {"auto_approve_up_to_pct": auto_approve_up_to_pct,
                                "auto_approve_up_to_usd": auto_approve_up_to_usd,
                                "always_ask_about": always_ask_about, "rule_note": rule_note}
        rule_source = "set by the assistant"
        if not any(rule.values()):
            choice = await ui.choose(
                "spending_rule",
                f"Booking: {svc.name} on {slot} for ${svc.price}. If the technician finds extra work on site, "
                "how should TrueUp handle the extra cost?", list(RULE_CHOICES))
            if ui.needs_input:
                return ui.input_required()
            if choice == DECLINED:
                return {"booked": False, "message": "No booking made: the user closed the spending-rule question."}
            if choice:
                rule.update(RULE_CHOICES[choice], rule_note=choice)
                rule_source = "chosen by the user (MCP elicitation)"
            else:
                rule_source = "default (PayPal limit); client could not ask the user"

        job = await arun(lambda: app.book(service_id, slot, problem, customer_ref="mcp-customer-agent", **rule))
        return {"booked": True, "job_id": job.job_id, "status": job.status.value, "slot": job.slot,
                "hold_amount": job.hold, "approve_url": job.approve_url,
                "spending_rule": _rule(job.mandate), "spending_rule_source": rule_source,
                "spending_rule_sha256": job.mandate_sha256,
                "next": "Ask the user to open approve_url and approve the hold at PayPal, then call "
                        "wait_for_paypal_approval."}

    @mcp.tool
    async def confirm_booking(job_id: str, ctx: Context | None = None) -> dict[str, Any]:
        """After the user approved the hold at PayPal: place the hold and confirm the visit."""
        app, ui = get_app(), Interaction(ctx)
        await ui.progress(1, 2, "Checking the hold approval at PayPal")
        job = await arun(lambda: app.confirm_hold(job_id))
        await ui.progress(2, 2, "Hold placed")
        return run(lambda: app.status(job.job_id))

    @mcp.tool
    async def wait_for_paypal_approval(
        job_id: str,
        timeout_seconds: Annotated[int, Field(ge=5, le=300, description="How long to wait")] = 90,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Wait (with live progress) while the user approves at PayPal - the booking hold or a price change -
        then finish that step automatically."""
        app, ui = get_app(), Interaction(ctx)
        job = run(lambda: app.store.require(job_id))
        if job.status == JobStatus.AWAITING_HOLD:
            order_id, what = job.order_id, "the booking hold"
        elif job.status == JobStatus.AWAITING_CUSTOMER:
            order_id, what = job.supplemental_order_id, "the price change"
        else:
            return {"waiting": False, "message": f"Nothing to approve; job is {job.status.value}.",
                    "status": run(lambda: app.status(job_id))}
        interval, waited = 3, 0
        while waited <= timeout_seconds:
            await ui.progress(waited, timeout_seconds, f"Waiting for you to approve {what} at PayPal ({waited}s)")
            status = (await arun(lambda: get_order(app.pp, order_id)))["status"]
            if status == "APPROVED":
                await ui.progress(timeout_seconds, timeout_seconds, f"Approved - finishing {what}")
                if job.status == JobStatus.AWAITING_HOLD:
                    await arun(lambda: app.confirm_hold(job_id))
                else:
                    await arun(lambda: app.approve_price_change(job_id))
                return {"approved": True, "status": run(lambda: app.status(job_id))}
            await asyncio.sleep(interval)
            waited += interval
        return {"approved": False, "approve_url": job.approve_url if job.status == JobStatus.AWAITING_HOLD
                else job.supplemental_approve_url,
                "message": f"Not approved yet after {timeout_seconds}s. The link still works; call this again."}

    @mcp.tool(annotations={"readOnlyHint": True})
    def get_job_status(job_id: str) -> dict[str, Any]:
        """Current status of a job and what happens next."""
        app = get_app()
        return run(lambda: app.status(job_id))

    # ---- tools: price changes -------------------------------------------------------------
    @mcp.tool(annotations={"readOnlyHint": True})
    async def view_price_change(job_id: str, ctx: Context | None = None) -> dict[str, Any] | mt.InputRequiredResult:
        """See an on-site price change: added items, reasons, new total, and a plain-language explanation."""
        app, ui = get_app(), Interaction(ctx)
        data = run(lambda: app.view_price_change(job_id))
        facts = (f"Service: {app.store.require(job_id).service_name}. Booked amount ${data['booked_amount']['amount']}. "
                 f"New total ${data['new_total']['amount']}, extra ${data['extra_to_approve']['amount']}. "
                 "Added on site: " + "; ".join(f"{i['name']} x{i['quantity']} (${i['unit_price']['amount']} each): "
                                               f"{i['reason']}" for i in data["added_items"]) +
                 f". Technician summary: {data['summary'] or 'n/a'}")
        text = await ui.sample(
            "explain", facts,
            system=("You explain home-repair price changes to the customer. Two short sentences, plain words, "
                    "state the extra amount exactly as given, add no facts that are not in the input."))
        if ui.needs_input:
            return ui.input_required()
        data["plain_explanation"] = text or data["message_from_business"] or data["summary"]
        data["explanation_source"] = ("your assistant, via MCP sampling" if text else "the business")
        return data

    @mcp.tool
    async def approve_price_change(job_id: str, ctx: Context | None = None) -> dict[str, Any] | mt.InputRequiredResult:
        """Approve an on-site price change. TrueUp first asks the USER to confirm, then returns the PayPal
        link for the extra amount. Once the user approved at PayPal, this (or wait_for_paypal_approval)
        charges the booked amount plus the approved extra."""
        app, ui = get_app(), Interaction(ctx)
        job = run(lambda: app.store.require(job_id))
        if job.status != JobStatus.AWAITING_CUSTOMER:
            raise ToolError(f"Job is {job.status.value}; there is no price change to approve.")
        await ui.progress(1, 3, "Checking the price change at PayPal")
        status = (await arun(lambda: get_order(app.pp, job.supplemental_order_id)))["status"]
        if status != "APPROVED":
            d = job.decision or {}
            added = ", ".join(i["name"] for i in job.items if i.get("added"))
            choice = await ui.choose(
                "confirm_price_change",
                f"{job.service_name} ({job_id}): the technician added {added}. Extra ${d['supplemental']['amount']} "
                f"(new total ${d['total']['amount']}, you pre-approved ${job.hold['amount']}). Do you approve?",
                [APPROVE_CHOICE, DECLINE_CHOICE])
            if ui.needs_input:
                return ui.input_required()
            if choice == DECLINE_CHOICE:
                await arun(lambda: app.decline_price_change(job_id))
                return {"done": True, "declined": True, "status": run(lambda: app.status(job_id))}
            if choice == DECLINED:
                return {"done": False, "message": "The user closed the question; nothing was changed."}
            return {"done": False, "user_confirmed": choice == APPROVE_CHOICE,
                    "approve_url": job.supplemental_approve_url,
                    "message": "Open approve_url to approve the extra amount at PayPal, then call "
                               "wait_for_paypal_approval."}
        await ui.progress(2, 3, "Charging the booked amount and the approved extra")
        result = await arun(lambda: app.approve_price_change(job_id))
        await ui.progress(3, 3, "Payment settled")
        return result

    @mcp.tool
    def decline_price_change(job_id: str) -> dict[str, Any]:
        """The user does not accept the price change: only the booked amount is charged."""
        app = get_app()
        return run(lambda: app.status(app.decline_price_change(job_id).job_id))

    @mcp.tool
    def cancel_booking(job_id: str) -> dict[str, Any]:
        """Cancel before the work is done: the hold is released and nothing is charged."""
        app = get_app()
        return run(lambda: app.status(app.cancel(job_id, "Cancelled by customer").job_id))

    # ---- resources -----------------------------------------------------------------------
    @mcp.resource("trueup://services", mime_type="application/json")
    def services_resource() -> dict[str, Any]:
        """Service catalog and visit prices."""
        app = get_app()
        return {"business": app.catalog.business, "services": app.list_services()}

    @mcp.resource("trueup://price-list", mime_type="application/json")
    def price_list_resource() -> dict[str, Any]:
        """Parts and labor price list used for on-site changes."""
        return get_app().price_list.model_dump()

    @mcp.resource("trueup://jobs/{job_id}/spending-rule", mime_type="application/json")
    def spending_rule_resource(job_id: str) -> dict[str, Any]:
        """The spending rule the customer agreed to for a job, and its SHA-256 fingerprint."""
        job = get_app().store.require(job_id)
        return {"mandate": job.mandate, "sha256": job.mandate_sha256}

    # ---- prompt ----------------------------------------------------------------------------
    @mcp.prompt
    def book_a_service(problem: str, when: str = "the next available slot") -> str:
        """Book a home-service visit with a safe spending rule."""
        return (f"I need help with: {problem}. Please book a visit for {when} using TrueUp. "
                "Tell me the visit price, let me choose how extra costs found on site are handled, and show me the "
                "PayPal link to approve the hold. Never approve a price change without asking me.")

    return mcp


def _rule(mandate: dict[str, Any]) -> dict[str, Any]:
    return {k: mandate.get(k) for k in ("max_auto_increase_pct", "max_auto_increase_amount",
                                        "always_ask_categories", "expires_at", "note")}


def main() -> None:
    from app.bootstrap import get_trueup

    server = create_server(get_trueup)
    if "--http" in sys.argv:
        server.run(transport="http", host="127.0.0.1", port=8001)
    else:
        server.run()  # stdio


if __name__ == "__main__":
    main()
