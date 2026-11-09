"""Ledger agent: the owner asks questions about jobs and money in plain English
("what did price changes add this week?", "which jobs are waiting on customers?").

Read-only: its tools only read the job store; the numbers are computed by code, the AI only
chooses which tool to call and phrases the answer.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain_core.tools import tool

from app.agents.middleware import AuditLogMiddleware
from app.store import Job, JobStatus, JobStore

LEDGER_PROMPT = """You answer the owner's questions about their jobs and money, using only the tools.
Never estimate or invent numbers: quote the figures the tools return (USD). Be brief: 1-4 sentences,
or a short list if they ask for one. If a question can't be answered from the tools, say so."""


def _dec(v: str | None) -> Decimal:
    return Decimal(v or "0")


def ledger_numbers(store: JobStore, days: int = 7, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    jobs = [j for j in store.list() if datetime.fromisoformat(j.created_at) >= since]
    settled = [j for j in jobs if j.status == JobStatus.SETTLED and j.settlement]
    charged = sum((_dec(j.settlement["charged"]) for j in settled), Decimal("0"))
    booked = sum((_dec(j.hold["amount"]) for j in settled), Decimal("0"))
    by_action: dict[str, int] = {}
    for j in settled:
        by_action[j.settlement["action"]] = by_action.get(j.settlement["action"], 0) + 1
    declined = sum(1 for j in settled if j.decision and j.decision["action"] == "NEEDS_APPROVAL"
                   and _dec(j.settlement["charged"]) == _dec(j.hold["amount"]))
    held = sum((_dec(j.hold["amount"]) for j in jobs if j.status in
                (JobStatus.BOOKED, JobStatus.AWAITING_OWNER, JobStatus.AWAITING_CUSTOMER)), Decimal("0"))
    return {
        "period_days": days,
        "jobs": len(jobs),
        "by_status": {s.value: sum(1 for j in jobs if j.status == s) for s in JobStatus if any(j.status == s for j in jobs)},
        "settled_jobs": len(settled),
        "total_charged": f"{charged:.2f}",
        "booked_amount_of_settled_jobs": f"{booked:.2f}",
        "net_from_on_site_changes": f"{charged - booked:.2f}",
        "settled_by_path": by_action,
        "price_changes_declined": declined,
        "money_on_hold_now": f"{held:.2f}",
    }


def _brief(j: Job) -> str:
    s = f"{j.job_id} | {j.status.value} | {j.service_name} | {j.slot} | booked ${j.hold['amount']}"
    if j.settlement:
        s += f" | charged ${j.settlement['charged']} ({j.settlement['action']})"
    elif j.decision:
        s += f" | new total ${j.decision['total']['amount']}"
    return s


def build_ledger_agent(model: Any, store: JobStore, audit: AuditLogMiddleware | None = None):
    @tool
    def ledger_summary(days: int = 7) -> dict[str, Any]:
        """Money and job totals for the last N days: charged, booked, net from on-site changes,
        how jobs settled (auto, approved, within hold, void), declines, money on hold now."""
        return ledger_numbers(store, max(1, min(days, 365)))

    @tool
    def list_jobs(status: str | None = None, limit: int = 20) -> str:
        """List recent jobs, optionally filtered by status: AWAITING_HOLD, BOOKED, AWAITING_OWNER,
        AWAITING_CUSTOMER, SETTLED, CANCELLED."""
        st = JobStatus(status.upper()) if status else None
        return "\n".join(_brief(j) for j in store.list(st)[:max(1, min(limit, 100))]) or "No jobs."

    @tool
    def job_detail(job_id: str) -> dict[str, Any]:
        """Items, decision reasons, settlement and timeline for one job."""
        j = store.require(job_id)
        return {"summary": _brief(j), "items": [f"{i['name']} x{i['quantity']} ${i['unit_amount']['amount']}"
                                                 for i in j.items],
                "reasons": (j.decision or {}).get("reasons"), "settlement": j.settlement,
                "events": [f"{e['at'][:16]} {e['kind']}" for e in store.events(job_id)]}

    return create_agent(model, tools=[ledger_summary, list_jobs, job_detail], system_prompt=LEDGER_PROMPT,
                        middleware=[ModelCallLimitMiddleware(run_limit=5), ToolCallLimitMiddleware(run_limit=6),
                                    audit or AuditLogMiddleware()], name="ledger_agent")


def ask_ledger(agent, question: str) -> str:
    out = agent.invoke({"messages": [{"role": "user", "content": question[:500]}]})
    return str(out["messages"][-1].content)
