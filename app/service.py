"""TrueUp service: the one place that runs a job through its life.

  customer's AI (MCP)            technician / owner (CLI now, web in Step 5)
  ---------------------          -------------------------------------------
  list_services / get_slots
  book()          -> AWAITING_HOLD  (PayPal order, intent=AUTHORIZE, approve link)
  confirm_hold()  -> BOOKED         (authorize after the customer approved)
                                    submit_report() -> bill agent -> decision engine
                                       VOID / CAPTURE_WITHIN / AUTO_CAPTURE -> SETTLED
                                       NEEDS_APPROVAL -> AWAITING_OWNER (owner agent pauses)
                                    owner_approve() -> supplemental order -> AWAITING_CUSTOMER
                                    owner_reject()  -> charge booked amount -> SETTLED
  view_price_change()
  approve_price_change() -> link, or capture both once approved -> SETTLED
  decline_price_change() -> charge booked amount -> SETTLED
  cancel()        -> void hold -> CANCELLED
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from app.agents.bill_agent import BillResult
from app.agents.owner_agent import OwnerDesk, pending_change_from
from app.agents.price_list import PriceList, default_price_list
from app.engine.decide import Action, Decision, decide
from app.engine.mandate import Mandate, custom_id_for
from app.engine.settle import (
    complete_price_change,
    decline_price_change,
    request_price_change,
    settle,
)
from app.paypal.client import PayPalClient, PayPalError
from app.paypal.models import LineItem, Money
from app.paypal.orders import authorize_order, create_authorize_order, get_order
from app.paypal.payments import void_authorization
from app.store import Job, JobFieldMap, JobStatus, JobStore

SERVICES_PATH = Path(__file__).resolve().parents[1] / "data" / "services.json"

# (transcript, photo bytes, mime, job context) -> BillResult
BillRunner = Callable[[str, bytes | None, str, str], BillResult]


class ServiceError(Exception):
    """A request that can't be done in the job's current state (shown to the caller as-is)."""


class Service(BaseModel):
    id: str
    name: str
    booking_sku: str
    price: str
    description: str


class Catalog(BaseModel):
    business: str
    area: str
    timezone: str
    slot_times: list[str]
    days_ahead: int
    services: list[Service]

    @classmethod
    def load(cls, path: Path = SERVICES_PATH) -> Catalog:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def get(self, service_id: str) -> Service:
        for s in self.services:
            if s.id == service_id.strip().lower():
                return s
        raise ServiceError(f"Unknown service '{service_id}'. Options: {', '.join(s.id for s in self.services)}")


def money(d: dict[str, str]) -> Money:
    return Money(amount=d["amount"], currency=d["currency"])


class TrueUp:
    def __init__(
        self,
        paypal: PayPalClient,
        store: JobStore,
        return_url: str,
        cancel_url: str,
        bill_runner: BillRunner | None = None,
        owner_model: Any = None,
        owner_checkpointer: Any = None,
        catalog: Catalog | None = None,
        price_list: PriceList | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        demo_mode: bool = False,
        public_base_url: str | None = None,
    ) -> None:
        self.demo_mode = demo_mode
        self.public_base_url = (public_base_url or "").rstrip("/")
        self.pp, self.store = paypal, store
        self.return_url, self.cancel_url = return_url, cancel_url
        self.bill_runner = bill_runner
        self.catalog = catalog or Catalog.load()
        self.price_list = price_list or default_price_list()
        self.clock = clock
        self._owner_model, self._owner_checkpointer = owner_model, owner_checkpointer
        self._desk: OwnerDesk | None = None

    # ---- catalog ------------------------------------------------------------
    def list_services(self) -> list[dict[str, str]]:
        return [s.model_dump() for s in self.catalog.services]

    def get_slots(self, service_id: str, day: str | None = None) -> list[str]:
        """Free slots (local time) for the next few days, or for one day (YYYY-MM-DD)."""
        self.catalog.get(service_id)
        tz = ZoneInfo(self.catalog.timezone)
        now = self.clock().astimezone(tz)
        earliest = now + timedelta(hours=1)
        taken = self.store.booked_slots(service_id)
        out = []
        if self.demo_mode:  # "ASAP": next quarter hour, so a demo can book, visit and bill in minutes
            q = (now + timedelta(minutes=15)).replace(second=0, microsecond=0)
            q = q.replace(minute=(q.minute // 15) * 15)
            asap = q.strftime("%Y-%m-%d %H:%M")
            if asap not in taken and (not day or asap.startswith(day)):
                out.append(asap)
        for d in range(0, self.catalog.days_ahead + 1):
            date = (now + timedelta(days=d)).date()
            if day and date.isoformat() != day:
                continue
            for t in self.catalog.slot_times:
                slot = f"{date.isoformat()} {t}"
                if slot in taken or slot in out or self.slot_start(slot) <= earliest:
                    continue
                out.append(slot)
        return out

    def slot_start(self, slot: str) -> datetime:
        return datetime.strptime(slot, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(self.catalog.timezone))

    # ---- booking ---------------------------------------------------------------
    def book(
        self,
        service_id: str,
        slot: str,
        problem: str,
        customer_ref: str = "customer-agent",
        auto_approve_up_to_pct: str | None = None,
        auto_approve_up_to_usd: str | None = None,
        always_ask_about: list[str] | None = None,
        rule_note: str | None = None,
        rule_valid_days: int = 14,
    ) -> Job:
        svc = self.catalog.get(service_id)
        if slot not in self.get_slots(service_id):
            raise ServiceError(f"Slot '{slot}' is not available. Use get_slots to pick a free one.")
        job_id = "job-" + uuid.uuid4().hex[:8]
        now = self.clock()
        mandate = Mandate(
            mandate_id="m-" + uuid.uuid4().hex[:8], customer_ref=customer_ref,
            max_auto_increase_pct=auto_approve_up_to_pct, max_auto_increase_amount=auto_approve_up_to_usd,
            always_ask_categories=always_ask_about or [], created_at=now,
            expires_at=now + timedelta(days=rule_valid_days), note=rule_note)
        hold = Money(amount=svc.price)
        custom_id = custom_id_for(job_id, mandate)
        order = create_authorize_order(self.pp, hold, f"{svc.name} - {slot}", custom_id,
                                       self.return_url, self.cancel_url, brand_name=self.catalog.business)
        job = Job(job_id=job_id, status=JobStatus.AWAITING_HOLD, service_id=svc.id, service_name=svc.name,
                  slot=slot, problem=problem[:500], customer_ref=customer_ref,
                  hold=hold.model_dump(mode="json"), mandate=mandate.model_dump(mode="json"),
                  mandate_sha256=mandate.sha256, custom_id=custom_id, order_id=order.id, approve_url=order.approve_url)
        self.store.save(job)
        self.store.event(job_id, "booked", {"slot": slot, "hold": str(hold), "mandate_sha256": mandate.sha256})
        return job

    def confirm_hold(self, job_id: str) -> Job:
        """Call after the customer approved the hold at PayPal. Places the authorization.
        Safe to call from several processes at once: only one places the hold."""
        job = self.store.require(job_id)
        if job.status != JobStatus.AWAITING_HOLD:
            return job
        if not self.store.claim(job_id, "hold"):
            return self._wait_while(job_id, JobStatus.AWAITING_HOLD)
        try:
            job = self.store.require(job_id)
            if job.status != JobStatus.AWAITING_HOLD:
                return job
            status = get_order(self.pp, job.order_id)["status"]
            if status != "APPROVED":
                raise ServiceError(f"The customer hasn't approved the hold yet (PayPal status {status}). "
                                   f"Approval link: {job.approve_url}")
            auth = authorize_order(self.pp, job.order_id, request_id=f"{job_id}:authorize")
            job.auth_id, job.auth_expires_at = auth.id, str(auth.expiration_time) if auth.expiration_time else None
            job.status = JobStatus.BOOKED
            self.store.save(job)
            self.store.event(job_id, "hold_placed", {"auth_id": auth.id, "amount": job.hold})
            return job
        finally:
            self.store.release(job_id, "hold")

    def _wait_while(self, job_id: str, status: JobStatus, timeout_s: float = 30.0) -> Job:
        """Another process holds the claim: wait for it to finish and return its result."""
        import time as _t
        deadline = _t.monotonic() + timeout_s
        job = self.store.require(job_id)
        while job.status == status and _t.monotonic() < deadline:
            _t.sleep(0.25)
            job = self.store.require(job_id)
        return job

    def cancel(self, job_id: str, reason: str = "Cancelled") -> Job:
        job = self.store.require(job_id)
        if job.status == JobStatus.AWAITING_HOLD:
            job.status = JobStatus.CANCELLED
        elif job.status == JobStatus.BOOKED:
            void_authorization(self.pp, job.auth_id)
            job.status = JobStatus.CANCELLED
            job.settlement = {"action": "VOID", "charged": "0.00", "summary": f"{reason}: hold released, nothing charged."}
        else:
            raise ServiceError(f"Job is {job.status.value}; it can't be cancelled now.")
        self.store.save(job)
        self.store.event(job_id, "cancelled", reason)
        return job

    # ---- technician report -> decision ----------------------------------------
    def submit_report(self, job_id: str, transcript: str, photo: bytes | None = None,
                      photo_mime: str = "image/jpeg", photo_path: str | None = None,
                      cancelled: bool = False) -> Job:
        job = self.store.require(job_id)
        if job.status != JobStatus.BOOKED:
            raise ServiceError(f"Job is {job.status.value}; a report can only be submitted for a BOOKED job.")
        if not self.demo_mode and self.clock() < self.slot_start(job.slot) - timedelta(minutes=30):
            raise ServiceError(f"The visit is booked for {job.slot} ({self.catalog.timezone}); a technician report "
                               "can only be filed once the visit has started.")
        job.reported_at = self.clock().isoformat()
        svc = self.catalog.get(job.service_id)
        visit = self.price_list.line_item(svc.booking_sku, added=False, reason="Booked visit")
        added: list[LineItem] = []
        result: BillResult | None = None
        if not cancelled:
            if self.bill_runner is None:
                raise ServiceError("No bill agent configured (OPENAI_API_KEY missing?).")
            result = self.bill_runner(transcript, photo, photo_mime, f"{svc.name}: {job.problem}")
            added = result.added_items
        items = [visit, *added]
        mandate = Mandate.model_validate(job.mandate)
        d = decide(money(job.hold), items, mandate, cancelled=cancelled, now=self.clock())

        job.transcript, job.photo_path = transcript, photo_path
        job.items = [i.model_dump(mode="json") for i in items]
        job.decision = d.model_dump(mode="json")
        if result:
            job.bill_summary = result.draft.summary if result.draft else None
            job.open_questions = result.draft.open_questions if result.draft else []
            job.dropped_lines = result.dropped
            self.store.event(job_id, "bill_drafted", {"items": [f"{i.name} x{i.quantity}" for i in added],
                                                      "dropped": result.dropped, "audit_steps": len(result.audit)})
        self.store.event(job_id, "decision", {"action": d.action.value, "total": str(d.total), "reasons": d.reasons})

        if d.action == Action.NEEDS_APPROVAL:
            job.status = JobStatus.AWAITING_OWNER
            self.store.save(job)
            desk = self.owner_desk()
            desk.start(pending_change_from(d, job_id, added, job.open_questions))
            return self.store.require(job_id)

        self.store.save(job)
        s = settle(self.pp, job.auth_id, d, invoice_id=job_id, note=job.bill_summary)
        return self._settled(job, s)

    # ---- owner -------------------------------------------------------------------
    def owner_desk(self) -> OwnerDesk:
        if self._desk is None:
            if self._owner_model is None:
                raise ServiceError("No owner agent model configured (OPENAI_API_KEY missing?).")
            kw = {"checkpointer": self._owner_checkpointer} if self._owner_checkpointer is not None else {}
            fieldmap = JobFieldMap(self.store, "owner_pending_amount")
            self._desk = OwnerDesk(model=self._owner_model, on_send=self._send_price_change,
                                   expected=fieldmap, amounts=fieldmap, **kw)
        return self._desk

    def owner_pending(self, job_id: str) -> dict[str, Any] | None:
        job = self.store.require(job_id)
        if job.status != JobStatus.AWAITING_OWNER:
            return None
        p = self.owner_desk().pending(job_id)
        if p:
            p.update(items=[i for i in job.items], reasons=(job.decision or {}).get("reasons", []),
                     open_questions=job.open_questions, total=(job.decision or {}).get("total"))
        return p

    def owner_approve(self, job_id: str, customer_message: str | None = None) -> Job:
        job = self.store.require(job_id)
        if job.status != JobStatus.AWAITING_OWNER:
            raise ServiceError(f"Job is {job.status.value}; nothing waiting for the owner.")
        desk = self.owner_desk()
        out = desk.approve_with_message(job_id, customer_message) if customer_message else desk.approve(job_id)
        self.store.event(job_id, "owner_approved", out[:300])
        return self.store.require(job_id)

    def owner_reject(self, job_id: str, reason: str = "Owner did not approve the extra work") -> Job:
        job = self.store.require(job_id)
        if job.status != JobStatus.AWAITING_OWNER:
            raise ServiceError(f"Job is {job.status.value}; nothing waiting for the owner.")
        self.owner_desk().reject(job_id, reason)
        self.store.event(job_id, "owner_rejected", reason)
        d = Decision.model_validate(job.decision)
        s = decline_price_change(self.pp, job.auth_id, d, invoice_id=job_id)
        return self._settled(self.store.require(job_id), s)

    def _send_price_change(self, job_id: str, customer_message: str) -> str:
        """Called by the owner agent's tool, only after the owner approved (HITL)."""
        job = self.store.require(job_id)
        d = Decision.model_validate(job.decision)
        items = [LineItem.model_validate(i) for i in job.items]
        order = request_price_change(self.pp, d, items, job.custom_id, self.return_url, self.cancel_url,
                                     owner_approved=True, description=customer_message[:127])
        job.status = JobStatus.AWAITING_CUSTOMER
        job.owner_message = customer_message
        job.supplemental_order_id, job.supplemental_approve_url = order.id, order.approve_url
        self.store.save(job)
        self.store.event(job_id, "price_change_sent", {"order_id": order.id, "amount": str(d.supplemental)})
        return f"Customer approval link: {order.approve_url}"

    # ---- customer: price change ------------------------------------------------------
    def view_price_change(self, job_id: str) -> dict[str, Any]:
        job = self.store.require(job_id)
        if job.status not in (JobStatus.AWAITING_OWNER, JobStatus.AWAITING_CUSTOMER):
            raise ServiceError(f"Job is {job.status.value}; there is no price change waiting.")
        d = job.decision or {}
        return {
            "job_id": job_id,
            "status": job.status.value,
            "booked_amount": job.hold,
            "new_total": d.get("total"),
            "extra_to_approve": d.get("supplemental"),
            "added_items": [{"name": i["name"], "quantity": i["quantity"], "unit_price": i["unit_amount"],
                             "reason": i.get("description")} for i in job.items if i.get("added")],
            "why_approval_is_needed": d.get("reasons", []),
            "message_from_business": job.owner_message,
            "summary": job.bill_summary,
            "visit_slot": f"{job.slot} ({self.catalog.timezone})",
            "work_reported_at": job.reported_at,
            "photo_evidence": bool(job.photo_path),
            "photo_url": (f"{self.public_base_url}/jobs/{job_id}/photo" if job.photo_path and self.public_base_url
                          else None),
            "receipt_url": f"{self.public_base_url}/jobs/{job_id}" if self.public_base_url else None,
            "approve_url": job.supplemental_approve_url,
            "spending_rule_sha256": job.mandate_sha256,
            "note": ("Waiting for the business owner to review it first."
                     if job.status == JobStatus.AWAITING_OWNER else
                     "Open approve_url to approve at PayPal, then call approve_price_change again to finish."),
        }

    def approve_price_change(self, job_id: str) -> dict[str, Any]:
        """Customer approved the extra at PayPal: capture it and the hold. Idempotent and safe to call
        from the return page, a webhook and the customer's assistant at the same time."""
        job = self.store.require(job_id)
        if job.status == JobStatus.SETTLED and job.supplemental_order_id:
            return {"done": True, "settlement": job.settlement}
        if job.status != JobStatus.AWAITING_CUSTOMER:
            raise ServiceError(f"Job is {job.status.value}; there is no price change to approve.")
        if not self.store.claim(job_id, "settle"):
            job = self._wait_while(job_id, JobStatus.AWAITING_CUSTOMER)
            return {"done": job.status == JobStatus.SETTLED, "settlement": job.settlement,
                    "approve_url": job.supplemental_approve_url}
        try:
            job = self.store.require(job_id)
            if job.status == JobStatus.SETTLED:
                return {"done": True, "settlement": job.settlement}
            status = get_order(self.pp, job.supplemental_order_id)["status"]
            if status not in ("APPROVED", "COMPLETED"):
                return {"done": False, "approve_url": job.supplemental_approve_url,
                        "message": f"Approve the extra amount at PayPal first (status {status}), then call this again."}
            d = Decision.model_validate(job.decision)
            s = complete_price_change(self.pp, job.auth_id, d, job.supplemental_order_id, invoice_id=job_id,
                                      note=job.bill_summary)
            job = self._settled(job, s)
            return {"done": True, "settlement": job.settlement}
        finally:
            self.store.release(job_id, "settle")

    def decline_price_change(self, job_id: str) -> Job:
        job = self.store.require(job_id)
        if job.status not in (JobStatus.AWAITING_CUSTOMER, JobStatus.AWAITING_OWNER):
            raise ServiceError(f"Job is {job.status.value}; there is no price change to decline.")
        if job.status == JobStatus.AWAITING_OWNER and self._owner_model is not None:
            self.owner_desk().reject(job_id, "Customer declined")
            job = self.store.require(job_id)
        d = Decision.model_validate(job.decision)
        s = decline_price_change(self.pp, job.auth_id, d, invoice_id=job_id)
        self.store.event(job_id, "customer_declined", "")
        return self._settled(job, s)

    # ---- PayPal approvals (return page + webhooks) ------------------------------------
    def order_approved(self, order_id: str, source: str = "return") -> Job | None:
        """The buyer approved a PayPal order (we learn it from the return page or a webhook).
        Advance whichever job it belongs to. Safe to call twice: already-advanced jobs are left alone."""
        job = self.store.find_by_order(order_id)
        if job is None:
            return None
        try:
            if job.status == JobStatus.AWAITING_HOLD and order_id == job.order_id:
                job = self.confirm_hold(job.job_id)
            elif job.status == JobStatus.AWAITING_CUSTOMER and order_id == job.supplemental_order_id:
                self.approve_price_change(job.job_id)
                job = self.store.require(job.job_id)
            else:
                return job
        except ServiceError:
            return self.store.require(job.job_id)
        self.store.event(job.job_id, "paypal_approval_seen", {"order_id": order_id, "via": source})
        return job

    def handle_webhook(self, event: dict[str, Any]) -> str:
        from app.paypal.webhooks import event_order_id

        etype = event.get("event_type", "")
        order_id = event_order_id(event)
        if etype == "CHECKOUT.ORDER.APPROVED" and order_id:
            job = self.order_approved(order_id, source="webhook")
            return f"advanced {job.job_id} -> {job.status.value}" if job else "unknown order"
        job = self.store.find_by_order(order_id) if order_id else None
        if job:
            res = event.get("resource") or {}
            self.store.event(job.job_id, f"webhook:{etype}", {"id": res.get("id"), "status": res.get("status"),
                                                              "amount": (res.get("amount") or {}).get("value")})
            return f"logged on {job.job_id}"
        return "ignored"

    # ---- status ----------------------------------------------------------------------
    def status(self, job_id: str) -> dict[str, Any]:
        job = self.store.require(job_id)
        if job.status == JobStatus.AWAITING_HOLD:
            try:
                job = self.confirm_hold(job_id)  # auto-advance if the customer has approved
            except (ServiceError, PayPalError):
                pass
        next_step = {
            JobStatus.AWAITING_HOLD: f"Customer approves the ${job.hold['amount']} hold at PayPal: {job.approve_url}",
            JobStatus.BOOKED: f"Technician visit at {job.slot}. Hold of ${job.hold['amount']} is in place.",
            JobStatus.AWAITING_OWNER: "The price changed on site; the business owner is reviewing it.",
            JobStatus.AWAITING_CUSTOMER: "A price change needs your approval: call view_price_change.",
            JobStatus.SETTLED: (job.settlement or {}).get("summary", "Settled."),
            JobStatus.CANCELLED: (job.settlement or {}).get("summary", "Cancelled."),
        }[job.status]
        return {"job_id": job.job_id, "status": job.status.value, "service": job.service_name, "slot": job.slot,
                "booked_amount": job.hold, "next_step": next_step,
                "spending_rule": {k: job.mandate.get(k) for k in
                                  ("max_auto_increase_pct", "max_auto_increase_amount", "always_ask_categories",
                                   "expires_at", "note")},
                "spending_rule_sha256": job.mandate_sha256, "settlement": job.settlement}

    def receipt(self, job_id: str) -> dict[str, Any]:
        job = self.store.require(job_id)
        return {"job": json.loads(job.model_dump_json()), "events": self.store.events(job_id)}

    # ---- helpers ----------------------------------------------------------------------
    def _settled(self, job: Job, s: Any) -> Job:
        """`job` must be the latest version (callers save or reload it first)."""
        job.status = JobStatus.SETTLED
        job.settlement = {"action": s.action.value, "charged": f"{s.charged.amount:.2f}", "voided": s.voided,
                          "summary": s.summary, "captures": [c.model_dump(mode="json") for c in s.captures]}
        self.store.save(job)
        self.store.event(job.job_id, "settled", {"charged": job.settlement["charged"], "summary": s.summary})
        return job
