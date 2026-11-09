"""Owner approval agent (Path 2: big price change).

The agent reads the decision + bill, writes a short plain-language message for the customer
and calls `send_price_change_to_customer`. That tool is gated by HumanInTheLoopMiddleware:
the run PAUSES until the owner approves, edits the message, or rejects. A checkpointer keeps
the paused job, so the owner can answer minutes or hours later (thread_id = job id).

The tool can't touch money: it has no amount argument at all. The amount comes from the
decision engine and is shown to the owner alongside the AI's message. Only on approval does `on_send` run (which creates the PayPal supplemental order).
"""
from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass, field
from typing import Any, Callable

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware, ModelCallLimitMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import tool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agents.middleware import AuditLogMiddleware, pii_middlewares
from app.engine.decide import Decision
from app.paypal.models import LineItem

SEND_TOOL = "send_price_change_to_customer"

OWNER_PROMPT = """You help the owner of a home-services business handle price changes on jobs.
You are given a job where the final bill is higher than the customer's pre-approved amount.
1. Write a short, friendly, factual message (max 3 sentences) to the customer explaining what was
   found, what was added and the extra amount. No pressure, no jargon.
2. Call send_price_change_to_customer with the job_id and your message. The amount is attached
   by the system. The owner will review it before anything is sent.
After the tool returns, reply with one line confirming what happened."""


@dataclass
class PendingChange:
    job_id: str
    extra_amount: str              # e.g. "51.00"
    items: list[LineItem]
    reasons: list[str]
    open_questions: list[str] = field(default_factory=list)


@dataclass
class OwnerDesk:
    """Holds the owner agent, its checkpointer and the jobs waiting for review."""

    model: BaseChatModel | str
    on_send: Callable[[str, str], str]         # (job_id, customer_message) -> approve link / status
    checkpointer: BaseCheckpointSaver = field(default_factory=InMemorySaver)
    audit: AuditLogMiddleware = field(default_factory=AuditLogMiddleware)
    # Any dict-like store works; the service passes database-backed maps so a paused approval
    # survives restarts and is shared between processes.
    expected: MutableMapping[str, str] = field(default_factory=dict)   # jobs still waiting to be sent
    amounts: MutableMapping[str, str] = field(default_factory=dict)    # job_id -> extra amount (from the engine)
    agent: Any = None

    def __post_init__(self) -> None:
        expected = self.expected
        on_send = self.on_send

        @tool(SEND_TOOL)
        def send_price_change_to_customer(job_id: str, customer_message: str) -> str:
            """Send the itemized price change to the customer's agent for approval.
            The amount is fixed by the system; you only write the message.
            Requires owner approval (the run pauses until the owner decides)."""
            if job_id not in expected:
                return f"Error: no pending price change for job {job_id}."
            result = on_send(job_id, customer_message)
            expected.pop(job_id, None)
            return f"Sent to customer. {result}"

        self.agent = create_agent(
            self.model,
            tools=[send_price_change_to_customer],
            system_prompt=OWNER_PROMPT,
            middleware=[
                *pii_middlewares(),
                ModelCallLimitMiddleware(run_limit=4, exit_behavior="end"),
                HumanInTheLoopMiddleware(interrupt_on={SEND_TOOL: {"allowed_decisions": ["approve", "edit", "reject"]}},
                                         description_prefix="Owner approval needed before the customer is asked"),
                self.audit,
            ],
            checkpointer=self.checkpointer,
            name="owner_agent",
        )

    @staticmethod
    def _cfg(job_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": f"owner-{job_id}"}}

    def start(self, change: PendingChange) -> dict[str, Any] | None:
        """Ask the agent to prepare the request. Returns the pending approval (what the owner sees)."""
        self.expected[change.job_id] = change.extra_amount
        self.amounts[change.job_id] = change.extra_amount
        lines = "\n".join(f"- {i.name} x{i.quantity}: ${i.total.amount} ({i.description or 'no note'})" for i in change.items)
        prompt = (f"job_id: {change.job_id}\nextra_amount: {change.extra_amount} USD\nAdded on site:\n{lines}\n"
                  f"Why approval is needed: {' '.join(change.reasons)}\n"
                  + (f"Owner should check: {'; '.join(change.open_questions)}" if change.open_questions else ""))
        out = self.agent.invoke({"messages": [{"role": "user", "content": prompt}]}, self._cfg(change.job_id), version="v2")
        return self.pending(change.job_id) if out.interrupts else None

    def pending(self, job_id: str) -> dict[str, Any] | None:
        state = self.agent.get_state(self._cfg(job_id))
        if not state.interrupts:
            return None
        req = state.interrupts[0].value["action_requests"][0]
        # The amount shown to the owner comes from the decision engine, never from the AI.
        return {"job_id": job_id, "tool": req["name"], "args": req["args"],
                "extra_amount": self.amounts.get(job_id)}

    def _resume(self, job_id: str, decision: dict[str, Any]) -> str:
        out = self.agent.invoke(Command(resume={"decisions": [decision]}), self._cfg(job_id), version="v2")
        msgs = out.value["messages"]
        tool_msgs = [m for m in msgs if getattr(m, "type", "") == "tool"]
        return (tool_msgs[-1].content if tool_msgs else msgs[-1].content) if msgs else ""

    def approve(self, job_id: str) -> str:
        return self._resume(job_id, {"type": "approve"})

    def approve_with_message(self, job_id: str, customer_message: str) -> str:
        args = dict(self.pending(job_id)["args"], customer_message=customer_message)
        return self._resume(job_id, {"type": "edit", "edited_action": {"name": SEND_TOOL, "args": args}})

    def reject(self, job_id: str, reason: str = "Owner rejected the price change") -> str:
        self.expected.pop(job_id, None)
        return self._resume(job_id, {"type": "reject", "message": reason})


def pending_change_from(decision: Decision, job_id: str, added_items: list[LineItem],
                        open_questions: list[str] | None = None) -> PendingChange:
    return PendingChange(job_id=job_id, extra_amount=f"{decision.supplemental.amount:.2f}", items=added_items,
                         reasons=decision.reasons, open_questions=open_questions or [])
