import asyncio
import re
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastmcp import Client
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite import SqliteSaver

from app.agents.bill_agent import BillResult
from app.agents.price_list import PriceList
from app.mcp_server.server import create_server
from app.paypal.client import PayPalClient
from app.service import ServiceError, TrueUp
from app.store import JobStatus, JobStore
from tests.fake_model import ScriptedModel
from tests.fake_paypal import FakePayPal

PL = PriceList.load()
NOW = datetime(2026, 11, 9, 18, 0, tzinfo=timezone.utc)


def bill_of(*skus):
    def runner(transcript, photo, mime, context):
        return BillResult(draft=None, added_items=[PL.line_item(s, reason=f"from note: {s}") for s in skus],
                          dropped=[], audit=[])
    return runner


class OwnerModel(ScriptedModel):
    """Calls the send tool for whatever job_id is in the prompt, then finishes."""

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        from langchain_core.outputs import ChatGeneration, ChatResult
        self.calls += 1
        if getattr(messages[-1], "type", "") == "tool":
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="Done."))])
        job_id = re.search(r"job_id: (\S+)", str(messages[-1].content)).group(1)
        msg = AIMessage(content="", tool_calls=[{"name": "send_price_change_to_customer", "id": f"t{self.calls}",
                                                 "args": {"job_id": job_id,
                                                          "customer_message": "We found a cracked cartridge: +$51."}}])
        return ChatResult(generations=[ChatGeneration(message=msg)])


@pytest.fixture
def pp(mock_api):
    return FakePayPal(mock_api)


@pytest.fixture
def make_app(settings, pp, tmp_path):
    db = str(tmp_path / "trueup.db")

    def make(runner=None, demo=True, clock=lambda: NOW):
        saver = SqliteSaver(sqlite3.connect(db, check_same_thread=False))
        return TrueUp(PayPalClient(settings), JobStore(db), "https://r", "https://c", bill_runner=runner,
                      owner_model=OwnerModel(responses=[AIMessage(content="x")]), owner_checkpointer=saver,
                      clock=clock, demo_mode=demo, public_base_url="https://trueup.test")
    return make


def booked(app, pp, **rule):
    slot = app.get_slots("plumbing")[0]
    job = app.book("plumbing", slot, "Kitchen sink leaking", **rule)
    assert job.status == JobStatus.AWAITING_HOLD and job.approve_url.endswith(job.order_id)
    with pytest.raises(ServiceError):
        app.confirm_hold(job.job_id)            # customer hasn't approved yet
    pp.approve(job.order_id)
    job = app.confirm_hold(job.job_id)
    assert job.status == JobStatus.BOOKED and job.auth_id
    return job


def test_slots_and_double_booking(make_app, pp):
    app = make_app()
    slots = app.get_slots("plumbing")
    # NOW = 10:00 Pacific on Nov 9: ASAP (demo) 10:15, today 14:00 + 16:00 (more than 1h away), then 5 days x 4
    assert slots[:4] == ["2026-11-09 10:15", "2026-11-09 14:00", "2026-11-09 16:00", "2026-11-10 09:00"]
    assert len(slots) == 23
    assert make_app(demo=False).get_slots("plumbing")[0] == "2026-11-09 14:00"
    app.book("plumbing", slots[0], "leak")
    assert slots[0] not in app.get_slots("plumbing")
    with pytest.raises(ServiceError):
        app.book("plumbing", slots[0], "leak")
    with pytest.raises(ServiceError):
        app.get_slots("roofing")


def test_hold_carries_job_and_mandate_hash(make_app, pp):
    app = make_app()
    job = booked(app, pp, auto_approve_up_to_usd="20", always_ask_about=["Emergency"])
    order = pp.orders[job.order_id]
    assert order["amount"] == "60.00" and order["intent"] == "AUTHORIZE"
    assert order["custom_id"] == f"{job.job_id}|m:{job.mandate_sha256[:16]}"
    assert job.mandate["always_ask_categories"] == ["emergency"]


def test_small_change_auto_settles(make_app, pp):
    app = make_app(bill_of("PLB-WSH"))
    job = booked(app, pp)
    job = app.submit_report(job.job_id, "replaced washer")
    assert job.status == JobStatus.SETTLED and job.settlement["charged"] == "66.00"
    assert pp.charged() == Decimal("66.00")


def test_cheaper_job_and_cancel(make_app, pp):
    app = make_app(bill_of())
    job = booked(app, pp)
    assert app.submit_report(job.job_id, "just tightened").settlement["charged"] == "60.00"
    job2 = booked(app, pp)
    assert app.cancel(job2.job_id).status == JobStatus.CANCELLED and pp.voided == [job2.auth_id]


def test_big_change_full_flow_survives_restart(make_app, pp):
    app = make_app(bill_of("PLB-WSH", "PLB-CRT"))
    job = booked(app, pp)
    job = app.submit_report(job.job_id, "washer and cartridge")
    assert job.status == JobStatus.AWAITING_OWNER and pp.charged() == 0

    # a different process (owner's CLI / web app) picks it up from the database
    app2 = make_app(bill_of())
    pending = app2.owner_pending(job.job_id)
    assert pending["extra_amount"] == "51.00" and pending["args"]["customer_message"].startswith("We found")
    view = app2.view_price_change(job.job_id)
    assert view["status"] == "AWAITING_OWNER" and view["approve_url"] is None

    job = app2.owner_approve(job.job_id)
    assert job.status == JobStatus.AWAITING_CUSTOMER and job.supplemental_approve_url
    sup = pp.orders[job.supplemental_order_id]
    assert sup["amount"] == "51.00" and [i["name"] for i in sup["items"]] == ["Faucet washer", "Faucet cartridge"]

    first = app2.approve_price_change(job.job_id)           # not approved at PayPal yet
    assert first["done"] is False and first["approve_url"] == job.supplemental_approve_url
    pp.approve(job.supplemental_order_id)
    done = app2.approve_price_change(job.job_id)
    assert done["done"] and done["settlement"]["charged"] == "111.00"
    assert pp.charged() == Decimal("111.00")
    kinds = [e["kind"] for e in app2.receipt(job.job_id)["events"]]
    assert kinds == ["booked", "hold_placed", "bill_drafted", "decision", "price_change_sent",
                     "owner_approved", "settled"]


def test_customer_declines(make_app, pp):
    app = make_app(bill_of("PLB-WSH", "PLB-CRT"))
    job = booked(app, pp)
    app.submit_report(job.job_id, "x")
    app.owner_approve(job.job_id)
    job = app.decline_price_change(job.job_id)
    assert job.settlement["charged"] == "60.00" and pp.charged() == Decimal("60.00")


def test_owner_rejects(make_app, pp):
    app = make_app(bill_of("PLB-WSH", "PLB-CRT"))
    job = booked(app, pp)
    app.submit_report(job.job_id, "x")
    job = app.owner_reject(job.job_id)
    assert job.status == JobStatus.SETTLED and pp.charged() == Decimal("60.00")
    assert not any("order" in c for c in pp.captures)   # no supplemental order was ever captured


def test_stricter_rule_makes_small_change_need_approval(make_app, pp):
    app = make_app(bill_of("PLB-WSH"))
    job = booked(app, pp, auto_approve_up_to_usd="0")
    assert app.submit_report(job.job_id, "washer").status == JobStatus.AWAITING_OWNER


def test_report_only_when_booked(make_app, pp):
    app = make_app(bill_of())
    job = app.book("plumbing", app.get_slots("plumbing")[0], "leak")
    with pytest.raises(ServiceError):
        app.submit_report(job.job_id, "x")


# ---- MCP server ------------------------------------------------------------------
def call(server, tool, args=None):
    async def go():
        async with Client(server) as c:
            return await c.call_tool(tool, args or {}, raise_on_error=False)
    return asyncio.run(go())


def test_mcp_lists_tools_resources_prompt(make_app):
    server = create_server(lambda: make_app())

    async def go():
        async with Client(server) as c:
            tools = {t.name for t in await c.list_tools()}
            resources = {str(r.uri) for r in await c.list_resources()}
            templates = {t.uri_template for t in await c.list_resource_templates()}
            prompts = {p.name for p in await c.list_prompts()}
            return tools, resources, templates, prompts
    tools, resources, templates, prompts = asyncio.run(go())
    assert tools == {"list_services", "get_slots", "book_visit", "confirm_booking", "wait_for_paypal_approval", "get_job_status",
                     "view_price_change", "approve_price_change", "decline_price_change", "cancel_booking"}
    assert {"trueup://services", "trueup://price-list"} <= resources
    assert "trueup://jobs/{job_id}/spending-rule" in templates
    assert prompts == {"book_a_service"}


def test_mcp_booking_flow(make_app, pp):
    app = make_app(bill_of("PLB-WSH", "PLB-CRT"))
    server = create_server(lambda: app)
    slot = call(server, "get_slots", {"service_id": "plumbing"}).data["slots"][0]
    booked_ = call(server, "book_visit", {"service_id": "plumbing", "slot": slot, "problem": "sink leak",
                                          "auto_approve_up_to_usd": "10", "rule_note": "small extras ok"}).data
    assert booked_["status"] == "AWAITING_HOLD" and booked_["approve_url"]
    assert booked_["spending_rule"]["max_auto_increase_amount"] == "10"

    not_yet = call(server, "confirm_booking", {"job_id": booked_["job_id"]})
    assert not_yet.is_error and "hasn't approved" in not_yet.content[0].text

    pp.approve(app.store.require(booked_["job_id"]).order_id)
    assert call(server, "get_job_status", {"job_id": booked_["job_id"]}).data["status"] == "BOOKED"

    app.submit_report(booked_["job_id"], "washer and cartridge")
    app.owner_approve(booked_["job_id"])
    view = call(server, "view_price_change", {"job_id": booked_["job_id"]}).data
    assert view["extra_to_approve"]["amount"] == "51.00" and view["message_from_business"]
    assert [i["name"] for i in view["added_items"]] == ["Faucet washer", "Faucet cartridge"]

    pp.approve(app.store.require(booked_["job_id"]).supplemental_order_id)
    done = call(server, "approve_price_change", {"job_id": booked_["job_id"]}).data
    assert done["done"] and done["settlement"]["charged"] == "111.00"


def test_mcp_errors_are_clean(make_app):
    server = create_server(lambda: make_app())
    r = call(server, "get_job_status", {"job_id": "job-nope"})
    assert r.is_error and "No job job-nope" in r.content[0].text


def test_report_before_visit_is_refused_outside_demo_mode(make_app, pp):
    from datetime import timedelta
    app = make_app(bill_of("PLB-WSH"), demo=False)
    slot = "2026-11-10 09:00"
    job = app.book("plumbing", slot, "leak")
    pp.approve(job.order_id)
    app.confirm_hold(job.job_id)
    with pytest.raises(ServiceError, match="once the visit has started"):
        app.submit_report(job.job_id, "washer")
    later = make_app(bill_of("PLB-WSH"), demo=False, clock=lambda: app.slot_start(slot) + timedelta(minutes=40))
    done = later.submit_report(job.job_id, "washer")
    assert done.status == JobStatus.SETTLED and done.reported_at


def test_view_price_change_shows_visit_time_and_links(make_app, pp):
    app = make_app(bill_of("PLB-WSH", "PLB-CRT"))
    job = booked(app, pp)
    app.submit_report(job.job_id, "x", photo=b"img", photo_path="data/uploads/x.jpg")
    v = app.view_price_change(job.job_id)
    assert v["visit_slot"].startswith(job.slot) and v["work_reported_at"]
    assert v["photo_url"] == f"https://trueup.test/jobs/{job.job_id}/photo"
    assert v["receipt_url"] == f"https://trueup.test/jobs/{job.job_id}"


def test_settling_is_idempotent_across_processes(make_app, pp):
    """The PayPal return page (web app) and the assistant's wait tool (MCP server) finish the same
    payment at the same moment: only one charge set may happen."""
    import threading
    app = make_app(bill_of("PLB-WSH", "PLB-CRT"))
    job = booked(app, pp)
    app.submit_report(job.job_id, "x")
    job = app.owner_approve(job.job_id)
    pp.approve(job.supplemental_order_id)

    web, mcp = make_app(), make_app()          # two independent processes sharing the database
    results = []
    threads = [threading.Thread(target=lambda a=a: results.append(a.approve_price_change(job.job_id)))
               for a in (web, mcp, web, mcp)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert all(r["done"] for r in results), results
    assert pp.charged() == Decimal("111.00") and len(pp.captures) == 2
    again = app.approve_price_change(job.job_id)          # called later: returns the same result
    assert again["done"] and again["settlement"]["charged"] == "111.00" and pp.charged() == Decimal("111.00")


def test_placing_the_hold_is_idempotent(make_app, pp):
    import threading
    app = make_app()
    job = app.book("plumbing", app.get_slots("plumbing")[0], "leak")
    pp.approve(job.order_id)
    out = []
    threads = [threading.Thread(target=lambda a=a: out.append(a.confirm_hold(job.job_id)))
               for a in (make_app(), make_app(), make_app())]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert {j.status for j in out} == {JobStatus.BOOKED} and len(pp.auths) == 1
    assert len({j.auth_id for j in out}) == 1


def test_repeated_capture_request_id_does_not_double_charge(make_app, pp):
    from app.engine.decide import Decision
    from app.engine.settle import settle
    app = make_app(bill_of("PLB-WSH"))
    job = booked(app, pp)
    job = app.submit_report(job.job_id, "washer")          # settles $66 with key "<job>:hold-capture"
    d = Decision.model_validate(job.decision)
    settle(app.pp, job.auth_id, d, invoice_id=job.job_id)  # a retry with the same key
    assert pp.charged() == Decimal("66.00") and len(pp.captures) == 1
