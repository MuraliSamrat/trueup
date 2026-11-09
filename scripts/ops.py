"""Technician + owner side of TrueUp, from the terminal (the web screens come in Step 5).

  python scripts/ops.py jobs                                   list jobs
  python scripts/ops.py status  JOB_ID                         status + next step
  python scripts/ops.py report  JOB_ID --text "replaced washer and cracked cartridge"
  python scripts/ops.py report  JOB_ID --audio note.m4a --photo sink.jpg
  python scripts/ops.py report  JOB_ID --cancelled             job not done: release the hold
  python scripts/ops.py owner   JOB_ID                         review a big change: approve / edit / reject
  python scripts/ops.py receipt JOB_ID                         full record + timeline
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from app.bootstrap import build  # noqa: E402
from app.service import ServiceError  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="TrueUp technician / owner tools")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("jobs")
    for name in ("status", "owner", "receipt"):
        sub.add_parser(name).add_argument("job_id")
    rp = sub.add_parser("report")
    rp.add_argument("job_id")
    rp.add_argument("--text")
    rp.add_argument("--audio")
    rp.add_argument("--photo")
    rp.add_argument("--cancelled", action="store_true")
    a = ap.parse_args()

    app = build()
    try:
        if a.cmd == "jobs":
            for j in app.store.list():
                print(f"{j.job_id}  {j.status.value:18} {j.service_name:20} {j.slot}  hold ${j.hold['amount']}")
        elif a.cmd == "status":
            print(json.dumps(app.status(a.job_id), indent=2, default=str))
        elif a.cmd == "receipt":
            print(json.dumps(app.receipt(a.job_id), indent=2, default=str))
        elif a.cmd == "report":
            transcript = a.text or ""
            if a.audio:
                from app.agents.openai_models import transcribe
                from app.config import get_settings
                print(f"Transcribing {a.audio} ...")
                transcript = transcribe(get_settings(), a.audio)
                print(f"  {transcript}")
            if not transcript and not a.cancelled:
                print("Give --text, --audio or --cancelled")
                return 1
            photo = Path(a.photo).read_bytes() if a.photo else None
            mime = (mimetypes.guess_type(a.photo)[0] if a.photo else None) or "image/jpeg"
            print("Running bill agent + decision engine ...")
            job = app.submit_report(a.job_id, transcript, photo, mime, photo_path=a.photo, cancelled=a.cancelled)
            for i in job.items:
                tag = "+" if i.get("added") else " "
                print(f"  {tag} {i['name']:32} x{i['quantity']}  ${i['unit_amount']['amount']}")
            d = job.decision or {}
            print(f"\nDecision: {d.get('action')}  total ${d.get('total', {}).get('amount')}")
            for r in d.get("reasons", []):
                print(f"  - {r}")
            print(f"\nStatus: {job.status.value}")
            if job.settlement:
                print(f"  {job.settlement['summary']}")
            if job.status.value == "AWAITING_OWNER":
                print(f"  Next: python scripts/ops.py owner {job.job_id}")
        elif a.cmd == "owner":
            p = app.owner_pending(a.job_id)
            if not p:
                print("Nothing waiting for the owner on this job.")
                return 0
            print(f"\n--- PRICE CHANGE FOR {a.job_id} ---")
            for i in p["items"]:
                if i.get("added"):
                    print(f"  + {i['name']:32} x{i['quantity']}  ${i['unit_amount']['amount']}  ({i.get('description')})")
            print(f"  New total ${p['total']['amount']}   extra to approve: ${p['extra_amount']} (from the decision engine)")
            for r in p["reasons"]:
                print(f"  - {r}")
            if p["open_questions"]:
                print(f"  Check: {p['open_questions']}")
            print(f"\n  AI's message to the customer:\n    {p['args']['customer_message']}")
            c = input("\n  [a]pprove / [e]dit message / [r]eject: ").strip().lower()[:1]
            if c == "r":
                job = app.owner_reject(a.job_id)
                print(f"Rejected. {job.settlement['summary']}")
            else:
                msg = input("  New message: ").strip() if c == "e" else None
                job = app.owner_approve(a.job_id, msg)
                print(f"Sent to the customer's AI. Status {job.status.value}.")
                print(f"Customer approves the extra here: {job.supplemental_approve_url}")
    except (ServiceError, KeyError) as e:
        print(f"Can't do that: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
