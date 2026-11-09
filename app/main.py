"""TrueUp web app (FastAPI).

  /                       how it works + MCP address
  /mcp                    the MCP server (streamable HTTP) for remote assistants
  /tech, /tech/jobs/{id}  technician: voice note / text + photo -> report       (staff login)
  /owner                  owner: approve / edit / reject price changes, ask the ledger  (staff login)
  /jobs/{id}              customer receipt: gauge, items, rule fingerprint, PayPal ids, timeline
  /paypal/return|cancel   where PayPal sends the buyer back; advances the job
  /webhooks/paypal        PayPal webhooks (signature verified with PayPal)
  /health

Run locally:  python -m uvicorn app.main:build_web --factory --port 8000
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Settings, get_settings
from app.engine.mandate import Mandate
from app.paypal.client import PayPalError
from app.paypal.models import Money, cap_for
from app.service import ServiceError, TrueUp
from app.store import Job, JobStatus

log = logging.getLogger("trueup.web")
WEB = Path(__file__).resolve().parent / "web"
UPLOADS = Path(__file__).resolve().parents[1] / "data" / "uploads"

EVENT_LABELS = {
    "booked": "Booked by the customer's assistant; PayPal hold requested",
    "hold_placed": "Customer approved; PayPal hold placed",
    "bill_drafted": "Technician's report priced by the bill agent",
    "decision": "Decision engine ran",
    "price_change_sent": "Owner approved; extra amount sent to the customer",
    "owner_approved": "Owner approval recorded",
    "owner_rejected": "Owner rejected the extra work",
    "customer_declined": "Customer declined the extra amount",
    "paypal_approval_seen": "PayPal approval received",
    "settled": "Settled with PayPal",
    "cancelled": "Cancelled; hold released",
}


WEBHOOK_LABELS = {
    "PAYMENT.AUTHORIZATION.CREATED": "PayPal confirmed the hold (verified webhook)",
    "PAYMENT.AUTHORIZATION.VOIDED": "PayPal confirmed the hold was released (verified webhook)",
    "PAYMENT.CAPTURE.COMPLETED": "PayPal confirmed a ${amount} payment (verified webhook)",
    "CHECKOUT.ORDER.APPROVED": "PayPal confirmed the customer's approval (verified webhook)",
}
# Recorded for debugging, but already told by another row (price_change_sent, hold_placed, settled).
HIDDEN_EVENTS = {"owner_approved", "paypal_approval_seen"}


def timeline(events: list[dict[str, Any]], job: Any, tz: str) -> list[dict[str, Any]]:
    """Receipt timeline rows: plain labels, business local time, no duplicate bookkeeping rows."""
    out = []
    for e in events:
        kind = e["kind"]
        if kind in HIDDEN_EVENTS:
            continue
        try:
            detail = json.loads(e["detail"]) if e["detail"] else {}
        except ValueError:
            detail = {}
        detail = detail if isinstance(detail, dict) else {}
        if kind.startswith("webhook:"):
            label = WEBHOOK_LABELS.get(kind[8:], f"PayPal webhook: {kind[8:]}").replace(
                "${amount}", f"${detail.get('amount')}" if detail.get("amount") else "")
            label = label.replace(" a  payment", " a payment")
        elif kind == "settled":
            charged = detail.get("charged")
            extra = job.supplemental_order_id is not None and Decimal(str(charged or 0)) > Decimal(job.hold["amount"])
            label = ("Customer approved the extra at PayPal; " if extra else "") + (
                f"settled with PayPal, ${charged} charged" if charged else "settled with PayPal")
            label = label[0].upper() + label[1:]
        else:
            label = EVENT_LABELS.get(kind, kind)
        at = datetime.fromisoformat(e["at"]).astimezone(ZoneInfo(tz))
        out.append({**e, "label": label, "local": f"{at:%b} {at.day}, {at.hour % 12 or 12}:{at:%M %p}"})
    return out


def gauge_model(hold: Money, total: Money, action: str | None, mandate: dict[str, Any] | None = None) -> dict[str, Any]:
    """Positions for the settlement gauge: booked hold, auto limit, final total."""
    auto = Mandate.model_validate(mandate).auto_limit(hold) if mandate else cap_for(hold)
    top = max(total.amount, cap_for(hold).amount, hold.amount * Decimal("1.4")) * Decimal("1.08")
    pct = lambda v: float(round(v / top * 100, 2))  # noqa: E731
    kind = {"VOID": "void", "CAPTURE_WITHIN": "", "AUTO_CAPTURE": "auto", "NEEDS_APPROVAL": "ask"}.get(action or "", "")
    caption = {
        "VOID": f"Cancelled: the ${hold.amount} hold was released and nothing was charged.",
        "CAPTURE_WITHIN": f"Final bill ${total.amount} is within the ${hold.amount} hold, so it was charged as is.",
        "AUTO_CAPTURE": f"Final bill ${total.amount} is within the customer's auto limit of ${auto.amount}, so it was charged without asking.",
        "NEEDS_APPROVAL": f"Final bill ${total.amount} is past the auto limit of ${auto.amount}, so the extra needs the owner's and the customer's approval.",
    }.get(action or "", f"Booked at ${hold.amount}. Auto limit ${auto.amount}.")
    t, h, a = total.amount, hold.amount, auto.amount
    segments = []  # where each dollar of the final bill landed
    if t > 0:
        segments.append({"cls": "seg-hold", "left": 0, "width": pct(min(t, h))})
    if t > h and a > h:
        segments.append({"cls": "seg-auto", "left": pct(h), "width": pct(min(t, a) - h)})
    if t > a:
        segments.append({"cls": "seg-ask", "left": pct(a), "width": pct(t - a)})
    return {"hold": f"{h:.2f}", "auto": f"{a:.2f}", "total": f"{t:.2f}",
            "hold_pct": pct(h), "auto_pct": pct(a), "total_pct": pct(t),
            "kind": kind, "caption": caption, "segments": segments}


def job_gauge(job: Job) -> dict[str, Any] | None:
    if not job.decision:
        return None
    d = job.decision
    g = gauge_model(Money(**job.hold), Money(**d["total"]), d["action"], job.mandate)
    if job.status == JobStatus.SETTLED and d["action"] == "NEEDS_APPROVAL" and job.settlement:
        charged = job.settlement["charged"]
        g["caption"] = (f"The final bill of ${g['total']} was past the auto limit of ${g['auto']}. "
                        + ("The owner and the customer approved the extra." if charged == g["total"]
                           else f"The extra was not approved, so only ${charged} was charged."))
    return g


def plain_reason(r: str) -> str:
    """Decision-engine reasons, reworded for customers and owners."""
    return (r.replace("PayPal's over-capture cap", "PayPal's automatic limit")
             .replace("(min of 115% and +$75)", "(115% of the booked amount, at most $75 more)")
             .replace("the customer's agent approves a supplemental order for", "the customer approves an extra payment of")
             .replace(" USD", ""))


def rule_text(m: dict[str, Any]) -> str:
    parts = []
    if m.get("max_auto_increase_amount") == "0":
        parts.append("Ask before any extra charge.")
    else:
        lim = []
        if m.get("max_auto_increase_pct"):
            lim.append(f"{m['max_auto_increase_pct']}%")
        if m.get("max_auto_increase_amount"):
            lim.append(f"${m['max_auto_increase_amount']}")
        parts.append("Charge extras automatically up to " + (" or ".join(lim) + ", within PayPal's 115% limit."
                                                              if lim else "PayPal's limit (115% of the booked amount, at most $75 more)."))
    if m.get("always_ask_categories"):
        parts.append("Always ask about: " + ", ".join(m["always_ask_categories"]) + ".")
    if m.get("note"):
        parts.append(f"In the customer's words: \"{m['note']}\"")
    if m.get("expires_at"):
        parts.append(f"Valid until {m['expires_at'][:10]}.")
    return " ".join(parts)


def create_app(get_app: Callable[[], TrueUp], settings: Settings,
               transcriber: Callable[[str], str] | None = None,
               ledger: Callable[[str], str] | None = None,
               mount_mcp: bool = True) -> FastAPI:
    lifespan = None
    mcp_app = None
    if mount_mcp:
        from app.mcp_server.server import create_server

        mcp_app = create_server(get_app).http_app(path="/mcp")
        lifespan = mcp_app.lifespan
    api = FastAPI(title="TrueUp", lifespan=lifespan, docs_url=None, redoc_url=None)
    api.mount("/static", StaticFiles(directory=WEB / "static"), name="static")
    templates = Jinja2Templates(directory=WEB / "templates")
    templates.env.filters["plain"] = plain_reason
    security = HTTPBasic(realm="TrueUp staff")
    base_url = settings.public_base_url.rstrip("/")

    def staff(creds: HTTPBasicCredentials = Depends(security)) -> str:
        ok_user = creds.username in ("owner", "tech")
        ok_pw = secrets.compare_digest(creds.password.encode(), settings.staff_password.get_secret_value().encode())
        if not (ok_user and ok_pw):
            raise HTTPException(401, "Sign in as owner or tech", headers={"WWW-Authenticate": 'Basic realm="TrueUp staff"'})
        return creds.username

    def render(request: Request, name: str, page: str = "", **ctx: Any) -> HTMLResponse:
        app = get_app()
        return templates.TemplateResponse(request, name, {"page": page, "business": app.catalog.business,
                                                          "demo_mode": app.demo_mode, **ctx})

    # ---- public --------------------------------------------------------------------
    @api.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "mcp": f"{base_url}/mcp"}

    @api.get("/", response_class=HTMLResponse)
    def home(request: Request):
        example = gauge_model(Money(amount="60"), Money(amount="111"), "NEEDS_APPROVAL")
        return render(request, "index.html", "home", example=example, mcp_url=f"{base_url}/mcp")

    @api.get("/jobs/{job_id}", response_class=HTMLResponse)
    def receipt(request: Request, job_id: str):
        app = get_app()
        job = app.store.get(job_id)
        if job is None:
            raise HTTPException(404, "No such job")
        events = timeline(app.store.events(job_id), job, app.catalog.timezone)
        return render(request, "receipt.html", job=job, g=job_gauge(job), events=events, tz=app.catalog.timezone,
                      rule_text=rule_text(job.mandate), next_step=app.status(job_id)["next_step"])

    @api.get("/jobs/{job_id}/photo")
    def photo(job_id: str):
        job = get_app().store.get(job_id)
        if not job or not job.photo_path or not Path(job.photo_path).is_file():
            raise HTTPException(404, "No photo")
        return FileResponse(job.photo_path)

    @api.get("/paypal/return", response_class=HTMLResponse)
    def paypal_return(request: Request, token: str = ""):
        app = get_app()
        job = None
        try:
            job = app.order_approved(token, source="return") if token else None
        except PayPalError:
            job = app.store.find_by_order(token)
        headline, detail = "Approved at PayPal.", ""
        if job is not None:
            if job.status == JobStatus.BOOKED:
                headline, detail = "Your visit is booked.", f"The ${job.hold['amount']} hold is in place for {job.slot}."
            elif job.status == JobStatus.SETTLED and job.settlement:
                headline, detail = "Payment complete.", job.settlement["summary"]
        return render(request, "paypal_return.html", job=job, headline=headline, detail=detail, cancelled=False)

    @api.get("/paypal/cancel", response_class=HTMLResponse)
    def paypal_cancel(request: Request):
        return render(request, "paypal_return.html", job=None, cancelled=True)

    @api.post("/webhooks/paypal")
    @api.post("/webhooks/paypal/", include_in_schema=False)  # PayPal does not follow redirects
    async def webhook(request: Request):
        from app.paypal.webhooks import verify_webhook

        app, raw = get_app(), await request.body()
        if not settings.paypal_webhook_id or not verify_webhook(app.pp, settings.paypal_webhook_id, request.headers, raw):
            return JSONResponse({"ok": False, "error": "signature not verified"}, status_code=400)
        return {"ok": True, "result": app.handle_webhook(json.loads(raw))}

    # ---- technician ------------------------------------------------------------------
    @api.get("/tech", response_class=HTMLResponse)
    def tech(request: Request, user: str = Depends(staff)):
        jobs = sorted(get_app().store.list(JobStatus.BOOKED), key=lambda j: j.slot)
        return render(request, "tech.html", "tech", jobs=jobs)

    @api.get("/tech/jobs/{job_id}", response_class=HTMLResponse)
    def tech_job(request: Request, job_id: str, user: str = Depends(staff)):
        job = get_app().store.get(job_id)
        if job is None:
            raise HTTPException(404, "No such job")
        return render(request, "tech_job.html", "tech", job=job)

    @api.post("/tech/jobs/{job_id}/report")
    def tech_report(request: Request, job_id: str, note: str = Form(""), cancelled: str = Form(""),
                    audio: UploadFile | None = File(None), photo: UploadFile | None = File(None),
                    user: str = Depends(staff)):
        # A plain `def`: FastAPI runs it in a worker thread, so the slow OpenAI + PayPal calls
        # don't block the server (or the MCP endpoint) while the report is priced.
        app = get_app()
        job = app.store.get(job_id)
        if job is None:
            raise HTTPException(404, "No such job")
        UPLOADS.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time())
        photo_bytes = photo_path = None
        photo_mime = "image/jpeg"
        step = "saving the upload"
        try:
            if photo is not None and photo.filename:
                photo_bytes = photo.file.read()
                photo_mime = photo.content_type or photo_mime
                if photo_mime not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
                    raise ServiceError(f"Photos must be JPEG, PNG, WebP or GIF (got {photo_mime}).")
                ext = Path(photo.filename).suffix.lower() or ".jpg"
                photo_path = str(UPLOADS / f"{job_id}-{stamp}{ext}")
                Path(photo_path).write_bytes(photo_bytes)
            transcript = note.strip()
            if audio is not None and audio.filename:
                audio_path = UPLOADS / f"{job_id}-{stamp}{Path(audio.filename).suffix or '.webm'}"
                audio_path.write_bytes(audio.file.read())
                if transcriber is None:
                    raise ServiceError("Voice notes need OPENAI_API_KEY; type the note instead.")
                step = "turning the voice note into text"
                spoken = transcriber(str(audio_path))
                transcript = f"{spoken}\n{transcript}".strip()
            if not transcript and not cancelled:
                raise ServiceError("Record a voice note or type what you did.")
            step = "pricing the work and settling with PayPal"
            app.submit_report(job_id, transcript, photo_bytes, photo_mime, photo_path=photo_path,
                              cancelled=bool(cancelled))
        except (ServiceError, PayPalError) as e:
            return render(request, "tech_job.html", "tech", job=app.store.require(job_id), error=str(e))
        except Exception as e:  # OpenAI, network, anything else: say what failed instead of a blank 500
            log.exception("Technician report failed for %s while %s", job_id, step)
            return render(request, "tech_job.html", "tech", job=app.store.require(job_id),
                          error=f"Something went wrong while {step}: {type(e).__name__}: {str(e)[:300]}. "
                                "Nothing was charged. You can type the note and send again.")
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    # ---- owner -------------------------------------------------------------------------
    def owner_page(request: Request, **extra: Any) -> HTMLResponse:
        from app.agents.ledger_agent import ledger_numbers

        app = get_app()
        pending = []
        for job in app.store.list(JobStatus.AWAITING_OWNER):
            try:
                p = app.owner_pending(job.job_id) or {}
            except ServiceError:
                p = {}
            pending.append({"job": job, "gauge": job_gauge(job),
                            "message": (p.get("args") or {}).get("customer_message", ""),
                            "extra": p.get("extra_amount") or (job.decision or {}).get("supplemental", {}).get("amount")})
        return render(request, "owner.html", "owner", pending=pending, jobs=app.store.list(),
                      summary=ledger_numbers(app.store), **extra)

    @api.get("/owner", response_class=HTMLResponse)
    def owner(request: Request, user: str = Depends(staff), flash: str = ""):
        return owner_page(request, flash=flash)

    @api.post("/owner/jobs/{job_id}/decide")
    def owner_decide(request: Request, job_id: str, decision: str = Form(...), message: str = Form(""),
                     user: str = Depends(staff)):
        app = get_app()
        try:
            if decision == "reject":
                job = app.owner_reject(job_id)
                flash = f"{job_id}: rejected. {job.settlement['summary']}"
            else:
                pending = app.owner_pending(job_id) or {}
                original = (pending.get("args") or {}).get("customer_message", "")
                edited = message.strip() if message.strip() and message.strip() != original.strip() else None
                job = app.owner_approve(job_id, edited)
                flash = f"{job_id}: sent to the customer for approval of the extra amount."
        except (ServiceError, PayPalError) as e:
            return owner_page(request, error=str(e))
        except Exception as e:
            log.exception("Owner decision failed for %s", job_id)
            return owner_page(request, error=f"Something went wrong: {type(e).__name__}: {str(e)[:300]}")
        return RedirectResponse(f"/owner?flash={flash}", status_code=303)

    @api.post("/owner/ask", response_class=HTMLResponse)
    def owner_ask(request: Request, question: str = Form(...), user: str = Depends(staff)):
        if ledger is None:
            return owner_page(request, question=question, answer="The ledger assistant needs OPENAI_API_KEY.")
        try:
            answer = ledger(question)
        except Exception as e:  # model/network error: say so plainly
            answer = f"The ledger assistant couldn't answer: {e}"
        return owner_page(request, question=question, answer=answer)

    if mcp_app is not None:
        api.mount("/", mcp_app)  # last, so the routes above take precedence; serves /mcp
    return api


def build_web() -> FastAPI:
    from app.bootstrap import get_trueup

    settings = get_settings()
    transcriber = ledger = None
    if settings.openai_api_key is not None:
        from app.agents.ledger_agent import ask_ledger, build_ledger_agent
        from app.agents.openai_models import transcribe

        transcriber = lambda path: transcribe(settings, path)  # noqa: E731
        agent = None

        def ledger(question: str) -> str:
            nonlocal agent
            if agent is None:
                app = get_trueup()
                agent = build_ledger_agent(app.ledger_model, app.store)
            return ask_ledger(agent, question)

    return create_app(get_trueup, settings, transcriber=transcriber, ledger=ledger)
