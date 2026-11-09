from datetime import datetime, timezone
from decimal import Decimal

from langchain_core.messages import AIMessage

from app.agents.bill_agent import build_bill_agent, draft_bill, price_draft, visit_message
from app.agents.middleware import AuditLogMiddleware, PriceGuardMiddleware, pii_middlewares
from app.agents.owner_agent import OwnerDesk, pending_change_from
from app.agents.price_list import PriceList
from app.agents.schemas import DraftBill, DraftLine
from app.engine import Action, Mandate, decide
from app.paypal.models import usd
from tests.fake_model import ScriptedModel, tool_call

PL = PriceList.load()
T0 = datetime(2026, 11, 9, tzinfo=timezone.utc)
NOTE = "Leak was a worn washer under the sink, replaced it. The faucet cartridge was cracked so I replaced that too."


def bill_call(lines, call_id, summary="Replaced worn washer and cracked cartridge."):
    return tool_call("DraftBill", {"lines": lines, "summary": summary}, call_id)


# ---- price list ------------------------------------------------------------
def test_price_list_search_and_line_item():
    assert PL.search("worn washer under the sink")[0].sku == "PLB-WSH"
    assert PL.search("ac humming not starting")[0].sku == "HVC-CAP"
    li = PL.line_item("plb-crt", 2, reason="cracked")
    assert (li.total.amount, li.category, li.added) == (Decimal("90.00"), "parts", True)
    assert PL.search("") == []


# ---- bill agent --------------------------------------------------------------
def test_bill_agent_happy_path_prices_come_from_code():
    model = ScriptedModel(responses=[
        tool_call("search_price_list", {"query": "worn washer"}, "s1"),
        tool_call("search_price_list", {"query": "cracked faucet cartridge"}, "s2"),
        bill_call([{"sku": "PLB-WSH", "quantity": 1, "reason": "worn washer"},
                   {"sku": "PLB-CRT", "quantity": 1, "reason": "cartridge cracked"}], "b1"),
    ])
    audit = AuditLogMiddleware()
    agent = build_bill_agent(model, PL, audit)
    r = draft_bill(agent, PL, audit, NOTE)
    assert [i.name for i in r.added_items] == ["Faucet washer", "Faucet cartridge"]
    assert sum(i.total.amount for i in r.added_items) == Decimal("51.00")
    assert r.dropped == []
    tools = [e for e in r.audit if e["kind"] == "tool"]
    assert [t["name"] for t in tools] == ["search_price_list", "search_price_list"]
    assert "PLB-WSH" in tools[0]["result_preview"]
    assert any(e["kind"] == "model" for e in r.audit)


def test_price_guard_sends_model_back_on_invented_sku():
    model = ScriptedModel(responses=[
        bill_call([{"sku": "WASHER-99", "quantity": 1, "reason": "washer"}], "b1"),
        bill_call([{"sku": "PLB-WSH", "quantity": 1, "reason": "washer"}], "b2"),
    ])
    audit = AuditLogMiddleware()
    r = draft_bill(build_bill_agent(model, PL, audit), PL, audit, "replaced washer")
    assert [i.name for i in r.added_items] == ["Faucet washer"]
    guard_msgs = [m for m in model.seen[-1] if getattr(m, "name", None) == "price_guard"]
    assert guard_msgs and "WASHER-99" in guard_msgs[0].content


def test_guard_gives_up_after_retries_and_pipeline_drops_bad_lines():
    bad = bill_call([{"sku": "FAKE-1", "quantity": 1, "reason": "?"},
                     {"sku": "PLB-WSH", "quantity": 1, "reason": "washer"}], "b")
    model = ScriptedModel(responses=[bad])
    audit = AuditLogMiddleware()
    r = draft_bill(build_bill_agent(model, PL, audit), PL, audit, "washer")
    assert [i.name for i in r.added_items] == ["Faucet washer"]
    assert r.dropped and r.dropped[0].startswith("FAKE-1")
    assert model.calls == 3  # first try + 2 guard retries


def test_guard_rules():
    g = PriceGuardMiddleware(PL)
    assert g.problems(DraftBill(lines=[DraftLine(sku="PLB-WSH", reason="x")], summary="s")) == []
    probs = g.problems(DraftBill(lines=[DraftLine(sku="VIS-STD", reason="x"), DraftLine(sku="PLB-WSH", quantity=15, reason="x")], summary="s"))
    assert len(probs) == 2


def test_pii_never_reaches_the_model():
    model = ScriptedModel(responses=[bill_call([{"sku": "PLB-WSH", "quantity": 1, "reason": "washer"}], "b1")])
    audit = AuditLogMiddleware()
    note = "Customer Sarah, 415-555-0134, sarah@example.com, 2211 North First Street. Replaced washer."
    draft_bill(build_bill_agent(model, PL, audit), PL, audit, note)
    sent = " ".join(str(m.content) for m in model.seen[0])
    assert "415-555-0134" not in sent and "sarah@example.com" not in sent and "2211 North First Street" not in sent
    assert "Replaced washer" in sent


def test_visit_message_with_photo():
    msg = visit_message("note", photo=b"\x89PNG...", photo_mime="image/png", job_context="Kitchen sink leak")
    assert msg["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "Kitchen sink leak" in msg["content"][0]["text"]


def test_price_draft_never_prices_the_booked_visit():
    items, dropped = price_draft(DraftBill(lines=[DraftLine(sku="VIS-STD", reason="visit")], summary="s"), PL)
    assert items == [] and dropped


# ---- bill -> decision engine ---------------------------------------------------
def test_agent_output_flows_into_decision_engine():
    added, _ = price_draft(DraftBill(lines=[DraftLine(sku="PLB-WSH", reason="w"), DraftLine(sku="PLB-CRT", reason="c")],
                                     summary="s"), PL)
    visit = PL.line_item("VIS-STD", added=False)
    m = Mandate(mandate_id="m", customer_ref="c", created_at=T0)
    d = decide(usd("60"), [visit, *added], m, now=T0)
    assert d.action == Action.NEEDS_APPROVAL and d.supplemental.amount == Decimal("51.00")
    small = decide(usd("60"), [visit, added[0]], m, now=T0)
    assert small.action == Action.AUTO_CAPTURE


# ---- owner agent (HITL) -------------------------------------------------------
def _desk(sent, reply_message="We found a cracked cartridge; the extra is $51."):
    model = ScriptedModel(responses=[
        tool_call("send_price_change_to_customer",
                  {"job_id": "job-1", "customer_message": reply_message}, "o1"),
        AIMessage(content="Done."),
    ])
    return OwnerDesk(model=model, on_send=lambda job, msg: sent.append((job, msg)) or "approve link: https://approve/SUP1")


def _change():
    added, _ = price_draft(DraftBill(lines=[DraftLine(sku="PLB-WSH", reason="w"), DraftLine(sku="PLB-CRT", reason="c")],
                                     summary="s"), PL)
    m = Mandate(mandate_id="m", customer_ref="c", created_at=T0)
    d = decide(usd("60"), [PL.line_item("VIS-STD", added=False), *added], m, now=T0)
    return pending_change_from(d, "job-1", added)


def test_owner_must_approve_before_anything_is_sent():
    sent = []
    desk = _desk(sent)
    pending = desk.start(_change())
    assert pending["extra_amount"] == "51.00"      # from the decision engine, not the AI
    assert "extra_amount" not in pending["args"]
    assert sent == []                       # paused: nothing sent yet
    assert desk.pending("job-1") is not None
    out = desk.approve("job-1")
    assert sent == [("job-1", "We found a cracked cartridge; the extra is $51.")]
    assert "approve link" in out
    assert desk.pending("job-1") is None


def test_owner_can_edit_message():
    sent = []
    desk = _desk(sent)
    desk.start(_change())
    desk.approve_with_message("job-1", "Hi! The cartridge was cracked, so we replaced it: +$51.")
    assert sent == [("job-1", "Hi! The cartridge was cracked, so we replaced it: +$51.")]


def test_owner_reject_sends_nothing():
    sent = []
    desk = _desk(sent)
    desk.start(_change())
    desk.reject("job-1", "Do the washer only")
    assert sent == []


def test_ai_has_no_way_to_set_the_amount():
    desk = OwnerDesk(model=ScriptedModel(responses=[AIMessage(content="x")]), on_send=lambda j, m: "ok")
    tool = desk.agent.nodes["tools"].bound._tools_by_name["send_price_change_to_customer"]
    assert set(tool.args) == {"job_id", "customer_message"}


def test_unknown_job_is_refused():
    sent = []
    model = ScriptedModel(responses=[
        tool_call("send_price_change_to_customer", {"job_id": "job-999", "customer_message": "x"}, "o1"),
        AIMessage(content="Done."),
    ])
    desk = OwnerDesk(model=model, on_send=lambda j, m: sent.append(j) or "ok")
    desk.start(_change())
    out = desk.approve("job-1")
    assert sent == [] and "no pending price change" in out
