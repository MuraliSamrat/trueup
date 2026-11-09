"""Build a fully wired TrueUp from settings (.env): PayPal, job store, OpenAI agents."""
from __future__ import annotations

import sqlite3
from functools import lru_cache

from app.agents.bill_agent import build_bill_agent, draft_bill
from app.agents.middleware import AuditLogMiddleware
from app.agents.price_list import default_price_list
from app.config import Settings, get_settings
from app.paypal.client import PayPalClient
from app.service import TrueUp
from app.store import DEFAULT_DB, JobStore


def build(settings: Settings | None = None, db_path: str | None = None) -> TrueUp:
    s = settings or get_settings()
    store = JobStore(db_path or DEFAULT_DB)
    bill_runner = owner_model = checkpointer = None

    if s.openai_api_key is not None:
        from langgraph.checkpoint.sqlite import SqliteSaver

        from app.agents.openai_models import chat_models

        primary, fallback = chat_models(s)
        pl = default_price_list()

        def bill_runner(transcript, photo, mime, context):  # noqa: ANN001
            audit = AuditLogMiddleware()
            agent = build_bill_agent(primary, pl, audit, fallback_model=fallback)
            return draft_bill(agent, pl, audit, transcript, photo, mime, job_context=context)

        owner_model = primary
        # Owner approvals pause for hours; keep them in the same SQLite file so they survive restarts
        # and can be answered from a different process (CLI, web app).
        checkpointer = SqliteSaver(sqlite3.connect(str(db_path or DEFAULT_DB), check_same_thread=False))

    app = TrueUp(PayPalClient(s), store, s.return_url, s.cancel_url, bill_runner=bill_runner,
                 owner_model=owner_model, owner_checkpointer=checkpointer,
                 demo_mode=s.demo_mode, public_base_url=s.public_base_url)
    app.ledger_model = owner_model  # used by the owner's "ask your ledger" box (None without OpenAI)
    return app


@lru_cache
def get_trueup() -> TrueUp:
    return build()
