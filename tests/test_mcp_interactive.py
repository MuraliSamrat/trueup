"""Elicitation, sampling and progress through a real MCP client, on the 2026-07-28 protocol
("auto") and the older 2025-11-25 one ("legacy"), plus clients that support none of them."""
import asyncio
import warnings

import pytest
from fastmcp import Client

import app.mcp_server.server as server_mod
from app.mcp_server.server import APPROVE_CHOICE, DECLINE_CHOICE, create_server
from app.store import JobStatus
from tests.test_service_mcp import bill_of  # noqa: F401  (fixtures below reuse helpers)
from tests.test_service_mcp import make_app, pp  # noqa: F401

warnings.filterwarnings("ignore")
MODES = ["auto", "legacy"]


class Recorder:
    def __init__(self, answers: dict[str, str] | None = None, sample_text: str = "Explained by the client's model."):
        self.answers = answers or {}
        self.sample_text = sample_text
        self.questions: list[str] = []
        self.samples: list[str] = []
        self.progress: list[tuple] = []

    async def elicit(self, message, response_type, params, context):
        self.questions.append(message)
        props = (getattr(params, "requested_schema", None) or {}).get("properties", {})
        key = next(iter(props), "value")
        options = props.get(key, {}).get("enum") or []
        want = next((v for k, v in self.answers.items() if k in message), options[0] if options else "")
        return {key: want}

    async def sample(self, messages, params, context):
        self.samples.append(messages[-1].content.text)
        return self.sample_text

    async def on_progress(self, progress, total, message):
        self.progress.append((progress, total, message))


def call(server, tool, args, rec: Recorder | None, mode: str):
    kw = {"mode": mode}
    if rec:
        kw.update(elicitation_handler=rec.elicit, sampling_handler=rec.sample)

    async def go():
        async with Client(server, **kw) as c:
            return await c.call_tool(tool, args, raise_on_error=False,
                                     progress_handler=rec.on_progress if rec else None)
    return asyncio.run(go())


def booked_job(app, pp, rule_usd="10"):
    job = app.book("plumbing", app.get_slots("plumbing")[0], "Kitchen sink leak", auto_approve_up_to_usd=rule_usd)
    pp.approve(job.order_id)
    return app.confirm_hold(job.job_id)


@pytest.mark.parametrize("mode", MODES)
def test_elicitation_lets_the_user_choose_the_spending_rule(make_app, pp, mode):
    app = make_app()
    server = create_server(lambda: app)
    rec = Recorder(answers={"how should TrueUp handle": "Auto-approve up to $10, ask me above that"})
    slot = app.get_slots("plumbing")[0]
    r = call(server, "book_visit", {"service_id": "plumbing", "slot": slot, "problem": "sink leak"}, rec, mode)
    assert not r.is_error, r.content
    assert r.data["booked"] and r.data["spending_rule"]["max_auto_increase_amount"] == "10"
    assert r.data["spending_rule_source"] == "chosen by the user (MCP elicitation)"
    assert len(rec.questions) == 1 and "$60.00" in rec.questions[0]
    assert len(app.store.list()) == 1          # booked exactly once, even with the retry round


@pytest.mark.parametrize("mode", MODES)
def test_no_elicitation_support_falls_back_to_default_rule(make_app, pp, mode):
    app = make_app()
    server = create_server(lambda: app)
    slot = app.get_slots("plumbing")[0]
    r = call(server, "book_visit", {"service_id": "plumbing", "slot": slot, "problem": "leak"}, None, mode)
    assert r.data["booked"] and r.data["spending_rule_source"].startswith("default")
    assert r.data["spending_rule"]["max_auto_increase_amount"] is None


def test_rule_given_by_assistant_skips_the_question(make_app, pp):
    app = make_app()
    rec = Recorder()
    r = call(create_server(lambda: app), "book_visit",
             {"service_id": "plumbing", "slot": app.get_slots("plumbing")[0], "problem": "leak",
              "auto_approve_up_to_usd": "0"}, rec, "auto")
    assert r.data["booked"] and rec.questions == []


@pytest.mark.parametrize("mode", MODES)
def test_sampling_writes_the_explanation(make_app, pp, mode):
    app = make_app(bill_of("PLB-WSH", "PLB-CRT"))
    job = booked_job(app, pp)
    app.submit_report(job.job_id, "washer and cartridge")
    app.owner_approve(job.job_id)
    rec = Recorder(sample_text="The plumber also replaced a cracked cartridge, so the extra is $51.00.")
    r = call(create_server(lambda: app), "view_price_change", {"job_id": job.job_id}, rec, mode)
    assert r.data["plain_explanation"] == rec.sample_text
    assert r.data["explanation_source"] == "your assistant, via MCP sampling"
    assert "extra $51.00" in rec.samples[0] and "Faucet cartridge" in rec.samples[0]


def test_no_sampling_support_uses_business_message(make_app, pp):
    app = make_app(bill_of("PLB-WSH", "PLB-CRT"))
    job = booked_job(app, pp)
    app.submit_report(job.job_id, "x")
    app.owner_approve(job.job_id)
    r = call(create_server(lambda: app), "view_price_change", {"job_id": job.job_id}, None, "auto")
    assert r.data["explanation_source"] == "the business" and "cartridge" in r.data["plain_explanation"]


@pytest.mark.parametrize("mode", MODES)
def test_price_change_confirmed_by_user_then_paid(make_app, pp, mode, monkeypatch):
    app = make_app(bill_of("PLB-WSH", "PLB-CRT"))
    job = booked_job(app, pp)
    app.submit_report(job.job_id, "x")
    job = app.owner_approve(job.job_id)
    server = create_server(lambda: app)
    rec = Recorder(answers={"Do you approve": APPROVE_CHOICE})

    r = call(server, "approve_price_change", {"job_id": job.job_id}, rec, mode)
    assert r.data["done"] is False and r.data["user_confirmed"] is True
    assert r.data["approve_url"] == job.supplemental_approve_url
    assert "Extra $51.00" in rec.questions[0]

    # the user approves at PayPal; wait_for_paypal_approval finishes it with live progress
    async def fast_sleep(_):
        pp.approve(job.supplemental_order_id)
    monkeypatch.setattr(server_mod.asyncio, "sleep", fast_sleep)
    rec2 = Recorder()
    done = call(server, "wait_for_paypal_approval", {"job_id": job.job_id, "timeout_seconds": 30}, rec2, mode)
    assert done.data["approved"] and done.data["status"]["status"] == "SETTLED"
    assert done.data["status"]["settlement"]["charged"] == "111.00"
    assert any("Waiting for you to approve the price change" in (m or "") for _, _, m in rec2.progress)
    assert any("Approved" in (m or "") for _, _, m in rec2.progress)


@pytest.mark.parametrize("mode", MODES)
def test_user_declines_in_the_question(make_app, pp, mode):
    app = make_app(bill_of("PLB-WSH", "PLB-CRT"))
    job = booked_job(app, pp)
    app.submit_report(job.job_id, "x")
    job = app.owner_approve(job.job_id)
    rec = Recorder(answers={"Do you approve": DECLINE_CHOICE})
    r = call(create_server(lambda: app), "approve_price_change", {"job_id": job.job_id}, rec, mode)
    assert r.data["declined"] and r.data["status"]["settlement"]["charged"] == "60.00"
    assert app.store.require(job.job_id).status == JobStatus.SETTLED


def test_wait_places_the_booking_hold_with_progress(make_app, pp, monkeypatch):
    app = make_app()
    job = app.book("plumbing", app.get_slots("plumbing")[0], "leak")

    async def fast_sleep(_):
        pp.approve(job.order_id)
    monkeypatch.setattr(server_mod.asyncio, "sleep", fast_sleep)
    rec = Recorder()
    r = call(create_server(lambda: app), "wait_for_paypal_approval", {"job_id": job.job_id}, rec, "auto")
    assert r.data["approved"] and r.data["status"]["status"] == "BOOKED"
    assert rec.progress[0][2].startswith("Waiting for you to approve the booking hold")


def test_wait_times_out_cleanly(make_app, pp, monkeypatch):
    app = make_app()
    job = app.book("plumbing", app.get_slots("plumbing")[0], "leak")

    async def fast_sleep(_):
        return None
    monkeypatch.setattr(server_mod.asyncio, "sleep", fast_sleep)
    r = call(create_server(lambda: app), "wait_for_paypal_approval",
             {"job_id": job.job_id, "timeout_seconds": 6}, Recorder(), "auto")
    assert r.data["approved"] is False and r.data["approve_url"] == job.approve_url
