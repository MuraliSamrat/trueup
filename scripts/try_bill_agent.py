"""Live check of Step 3 with your OpenAI key (no PayPal needed).

  python scripts/try_bill_agent.py                       # uses a sample plumber note
  python scripts/try_bill_agent.py --text "replaced the AC capacitor and cleaned the coil"
  python scripts/try_bill_agent.py --audio note.m4a --photo sink.jpg

It: transcribes the voice note (if given) -> runs the bill agent (with all middleware)
-> prices the lines from the price list -> runs the decision engine on a $60 hold
-> if the change is big, runs the owner agent and pauses for YOUR approval in the terminal.
Saves scripts/try_bill_agent_result.json (no secrets).
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from pydantic_settings import BaseSettings, SettingsConfigDict  # noqa: E402

from app.agents.bill_agent import build_bill_agent, draft_bill  # noqa: E402
from app.agents.middleware import AuditLogMiddleware  # noqa: E402
from app.agents.openai_models import chat_models, transcribe  # noqa: E402
from app.agents.owner_agent import OwnerDesk, pending_change_from  # noqa: E402
from app.agents.price_list import PriceList  # noqa: E402
from app.engine import Action, Mandate, decide  # noqa: E402
from app.paypal.models import usd  # noqa: E402

SAMPLE = ("Hi, this is Mike at the Sarah job, 415-555-0134. The leak was a worn washer under the kitchen sink, "
          "I replaced it. While I was in there the faucet cartridge was cracked, so I swapped that too. All dry now.")


class AISettings(BaseSettings):
    """Only the OpenAI part of the config (PayPal keys not needed for this script)."""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"
    openai_fallback_model: str = "gpt-4o-mini"
    openai_transcribe_model: str = "gpt-4o-mini-transcribe"

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings,
                                   file_secret_settings):
        return init_settings, dotenv_settings, env_settings, file_secret_settings  # .env wins


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text")
    ap.add_argument("--audio")
    ap.add_argument("--photo")
    args = ap.parse_args()

    s = AISettings()
    if not s.openai_api_key:
        print("Add OPENAI_API_KEY=... to your .env file first.")
        return 1
    from pydantic import SecretStr
    s.openai_api_key = SecretStr(s.openai_api_key)  # type: ignore[assignment]

    if args.audio:
        print(f"Transcribing {args.audio} ...")
        transcript = transcribe(s, args.audio)  # type: ignore[arg-type]
    else:
        transcript = args.text or SAMPLE
    print(f"\nTechnician note:\n  {transcript}\n")

    photo = Path(args.photo).read_bytes() if args.photo else None
    mime = (mimetypes.guess_type(args.photo)[0] if args.photo else None) or "image/jpeg"

    pl = PriceList.load()
    primary, fallback = chat_models(s)  # type: ignore[arg-type]
    audit = AuditLogMiddleware()
    agent = build_bill_agent(primary, pl, audit, fallback_model=fallback)
    print(f"Running bill agent ({s.openai_model}, fallback {s.openai_fallback_model}) ...")
    r = draft_bill(agent, pl, audit, transcript, photo, mime, job_context="Kitchen sink leak, standard visit $60")

    print("\nAI bill (priced by code from the price list):")
    for i in r.added_items:
        print(f"  + {i.name:32} x{i.quantity}  ${i.total.amount}   ({i.description})")
    if r.dropped:
        print("  Flagged for owner (not priced):", r.dropped)
    if r.draft:
        print(f"\nSummary for customer: {r.draft.summary}")
        if r.draft.open_questions:
            print(f"Owner should check: {r.draft.open_questions}")

    visit = pl.line_item("VIS-STD", added=False)
    mandate = Mandate(mandate_id="demo", customer_ref="agent-demo", created_at=datetime.now(timezone.utc),
                      note="Small extras are fine, ask me for anything big.")
    d = decide(usd("60"), [visit, *r.added_items], mandate)
    print(f"\nDecision: {d.action.value}  total ${d.total.amount} on a $60 hold (cap ${d.paypal_cap.amount})")
    for reason in d.reasons:
        print(f"  - {reason}")

    owner_result = None
    if d.action == Action.NEEDS_APPROVAL:
        desk = OwnerDesk(model=primary, on_send=lambda job, msg: "(demo: PayPal supplemental order would be created here)")
        pending = desk.start(pending_change_from(d, "demo-job", r.added_items,
                                                 r.draft.open_questions if r.draft else []))
        if pending:
            print("\n--- OWNER APPROVAL NEEDED ---")
            print(f"  Extra amount: ${pending['extra_amount']}  (set by the decision engine, not the AI)")
            print(f"  Message to customer: {pending['args']['customer_message']}")
            choice = input("  Approve? [a]pprove / [e]dit message / [r]eject: ").strip().lower()[:1]
            if choice == "e":
                owner_result = desk.approve_with_message("demo-job", input("  New message: "))
            elif choice == "r":
                owner_result = desk.reject("demo-job", "Owner rejected")
            else:
                owner_result = desk.approve("demo-job")
            print(f"  Result: {owner_result}")

    out = ROOT / "scripts" / "try_bill_agent_result.json"
    out.write_text(json.dumps({
        "transcript_sent_to_ai_redacted": True,
        "items": [i.model_dump(mode="json") for i in r.added_items],
        "dropped": r.dropped,
        "draft": r.draft.model_dump() if r.draft else None,
        "decision": d.model_dump(mode="json"),
        "owner_result": owner_result,
        "audit": r.audit,
    }, indent=2, default=str))
    print(f"\nSaved {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
