from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.agents.ledger_agent import ask_ledger, build_ledger_agent, ledger_numbers
from app.config import Settings
from app.main import create_app, gauge_model, rule_text
from app.paypal.models import Money
from app.store import JobStatus
from tests.fake_model import ScriptedModel, tool_call
from tests.test_service_mcp import bill_of, booked, make_app, pp  # noqa: F401

AUTH = ("owner", "pw")


@pytest.fixture
def web(make_app, pp):
    def build(runner=None, transcriber=None, ledger=None, webhook_id=None):
        app = make_app(runner)
        s = Settings(paypal_client_id="x", paypal_client_secret="y", staff_password="pw",
                     public_base_url="https://trueup.test", paypal_webhook_id=webhook_id, _env_file=None)
        client = TestClient(create_app(lambda: app, s, transcriber=transcriber, ledger=ledger, mount_mcp=False))
        return app, client
    return build


def test_gauge_and_rule_text():
    g = gauge_model(Money(amount="60"), Money(amount="111"), "NEEDS_APPROVAL")
    assert (g["hold"], g["auto"], g["total"], g["kind"]) == ("60.00", "69.00", "111.00", "ask")
    assert 0 < g["hold_pct"] < g["auto_pct"] < g["total_pct"] <= 100
    assert "Ask before any extra charge" in rule_text({"max_auto_increase_amount": "0"})
    assert "PayPal's limit" in rule_text({})


def test_home_and_health(web):
    _, c = web()
    r = c.get("/")
    assert r.status_code == 200 and "TrueUp settles the real bill" in r.text and "https://trueup.test/mcp" in r.text
    assert c.get("/health").json()["mcp"] == "https://trueup.test/mcp"


def test_staff_pages_need_login(web):
    _, c = web()
    assert c.get("/owner").status_code == 401
    assert c.get("/tech", auth=("tech", "wrong")).status_code == 401
    assert c.get("/tech", auth=("tech", "pw")).status_code == 200


def test_technician_report_to_owner_to_customer(web, pp):
    app, c = web(runner=bill_of("PLB-WSH", "PLB-CRT"))
    job = booked(app, pp)
    assert job.job_id in c.get("/tech", auth=AUTH).text
    r = c.post(f"/tech/jobs/{job.job_id}/report", auth=AUTH, data={"note": "washer and cartridge"},
               files={"photo": ("sink.jpg", b"\xff\xd8fakejpeg", "image/jpeg")}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/jobs/{job.job_id}"
    job = app.store.require(job.job_id)
    assert job.status == JobStatus.AWAITING_OWNER and job.photo_path
    assert c.get(f"/jobs/{job.job_id}/photo").content == b"\xff\xd8fakejpeg"

    page = c.get("/owner", auth=AUTH).text
    assert "Price changes to review" in page and "We found a cracked cartridge" in page and "$51.00" in page
    r = c.post(f"/owner/jobs/{job.job_id}/decide", auth=AUTH, follow_redirects=False,
               data={"decision": "approve", "message": "Hi! The cartridge was cracked: +$51."})
    assert r.status_code == 303
    job = app.store.require(job.job_id)
    assert job.status == JobStatus.AWAITING_CUSTOMER and job.owner_message == "Hi! The cartridge was cracked: +$51."

    # customer approves at PayPal and lands on our return page
    pp.approve(job.supplemental_order_id)
    ret = c.get(f"/paypal/return?token={job.supplemental_order_id}")
    assert "Payment complete." in ret.text
    receipt = c.get(f"/jobs/{job.job_id}").text
    for text in ("Faucet cartridge", "$111.00", job.mandate_sha256, "Settled with PayPal", "Photo from the job"):
        assert text in receipt


def test_return_page_places_booking_hold(web, pp):
    app, c = web()
    job = app.book("plumbing", app.get_slots("plumbing")[0], "leak")
    pp.approve(job.order_id)
    r = c.get(f"/paypal/return?token={job.order_id}&PayerID=X")
    assert "Your visit is booked." in r.text and app.store.require(job.job_id).status == JobStatus.BOOKED
    assert "Your visit is booked." in c.get(f"/paypal/return?token={job.order_id}").text  # idempotent
    assert "Nothing was approved." in c.get("/paypal/cancel").text


def test_owner_reject_and_report_errors(web, pp):
    app, c = web(runner=bill_of("PLB-WSH", "PLB-CRT"))
    job = booked(app, pp)
    err = c.post(f"/tech/jobs/{job.job_id}/report", auth=AUTH, data={"note": ""})
    assert "Record a voice note or type what you did." in err.text
    c.post(f"/tech/jobs/{job.job_id}/report", auth=AUTH, data={"note": "x"})
    c.post(f"/owner/jobs/{job.job_id}/decide", auth=AUTH, data={"decision": "reject"})
    assert app.store.require(job.job_id).settlement["charged"] == "60.00"


def test_voice_note_is_transcribed(web, pp):
    heard = []
    app, c = web(runner=bill_of("PLB-WSH"), transcriber=lambda p: heard.append(p) or "Replaced the washer.")
    job = booked(app, pp)
    c.post(f"/tech/jobs/{job.job_id}/report", auth=AUTH,
           files={"audio": ("voice-note.webm", b"webm-bytes", "audio/webm")}, data={"note": ""})
    job = app.store.require(job.job_id)
    assert heard and heard[0].endswith(".webm") and job.transcript == "Replaced the washer."
    assert job.status == JobStatus.SETTLED and job.settlement["charged"] == "66.00"


def test_cancel_from_technician_page(web, pp):
    app, c = web()
    job = booked(app, pp)
    c.post(f"/tech/jobs/{job.job_id}/report", auth=AUTH, data={"note": "", "cancelled": "1"})
    assert app.store.require(job.job_id).settlement["action"] == "VOID" and pp.voided == [job.auth_id]


def test_webhook_requires_valid_signature(web, pp, mock_api):
    app, c = web(webhook_id="WH-1")
    job = app.book("plumbing", app.get_slots("plumbing")[0], "leak")
    event = {"event_type": "CHECKOUT.ORDER.APPROVED", "resource": {"id": job.order_id}}
    assert c.post("/webhooks/paypal", json=event).status_code == 400          # no signature headers
    assert c.post("/webhooks/paypal/", json=event, follow_redirects=False).status_code == 400  # slash: no redirect

    mock_api.post("/v1/notifications/verify-webhook-signature").mock(
        return_value=httpx.Response(200, json={"verification_status": "SUCCESS"}))
    pp.approve(job.order_id)
    headers = {"paypal-auth-algo": "SHA256withRSA", "paypal-cert-url": "https://api.paypal.com/cert",
               "paypal-transmission-id": "t1", "paypal-transmission-sig": "sig", "paypal-transmission-time": "now"}
    r = c.post("/webhooks/paypal", json=event, headers=headers)
    assert r.json()["result"].endswith("-> BOOKED")


def test_ledger_numbers_and_agent(make_app, pp):
    app = make_app(bill_of("PLB-WSH"))
    job = booked(app, pp)
    app.submit_report(job.job_id, "washer")
    n = ledger_numbers(app.store, now=datetime(2026, 11, 10, tzinfo=timezone.utc))
    assert (n["settled_jobs"], n["total_charged"], n["net_from_on_site_changes"]) == (1, "66.00", "6.00")

    model = ScriptedModel(responses=[tool_call("ledger_summary", {"days": 7}, "l1"),
                                     AIMessage(content="Price changes added $6.00 this week.")])
    agent = build_ledger_agent(model, app.store)
    assert ask_ledger(agent, "What did changes add?") == "Price changes added $6.00 this week."
    tool_result = [m for m in model.seen[-1] if getattr(m, "type", "") == "tool"][0].content
    assert '"total_charged": "66.00"' in tool_result or "'total_charged': '66.00'" in tool_result


def test_owner_ask_uses_ledger(web):
    _, c = web(ledger=lambda q: f"You asked: {q}")
    r = c.post("/owner/ask", auth=AUTH, data={"question": "Which jobs are waiting?"})
    assert "You asked: Which jobs are waiting?" in r.text


def test_unexpected_errors_are_shown_not_500(web, pp):
    def boom(*a):
        raise RuntimeError("OpenAI said no")
    app, c = web(runner=boom)
    job = booked(app, pp)
    r = c.post(f"/tech/jobs/{job.job_id}/report", auth=AUTH, data={"note": "washer"})
    assert r.status_code == 200 and "while pricing the work" in r.text and "OpenAI said no" in r.text
    assert app.store.require(job.job_id).status == JobStatus.BOOKED   # nothing changed, nothing charged
    bad = c.post(f"/tech/jobs/{job.job_id}/report", auth=AUTH, data={"note": "x"},
                 files={"photo": ("p.heic", b"x", "image/heic")})
    assert "Photos must be JPEG" in bad.text
