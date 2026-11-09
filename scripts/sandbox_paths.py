"""Live PayPal sandbox run of TrueUp's three settlement paths.

  Path 1 - small change : $60 hold -> plumber adds a $6 washer -> auto-capture $66 (cap is $69)
  Path 2 - big change   : $60 hold -> washer $6 + faucet cartridge $45 = $111 (above cap)
                          -> supplemental order for $51 (itemized), customer approves,
                          -> capture the $51 order + capture the $60 hold
  Path 3 - cancelled    : $60 hold -> void, customer pays $0

Needs a human: approve each PayPal page while logged in as the US sandbox PERSONAL account.
Reads PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET from .env (or asks for them).
Writes scripts/sandbox_paths_result.json (no secrets).
"""
from __future__ import annotations

import getpass
import json
import os
import sys
import uuid
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # so .env in the project root is found

from app.config import Settings  # noqa: E402
from app.paypal import (  # noqa: E402
    LineItem,
    PayPalClient,
    PayPalError,
    authorize_order,
    cap_for,
    capture_authorization,
    capture_order,
    create_authorize_order,
    create_supplemental_order,
    usd,
    void_authorization,
)

RESULT_FILE = ROOT / "scripts" / "sandbox_paths_result.json"
HOLD = usd("60")
WASHER = LineItem(name="Washer (under-sink leak)", unit_amount=usd("6"))
CARTRIDGE = LineItem(name="Faucet cartridge (cracked)", unit_amount=usd("45"))

results: dict = {"started_at": datetime.now(timezone.utc).isoformat(), "paths": {}}


def load_settings() -> Settings:
    if not (os.environ.get("PAYPAL_CLIENT_ID") or (ROOT / ".env").exists()):
        print("No .env found. Paste the TrueUp app's SANDBOX keys (right-click pastes).")
        os.environ["PAYPAL_CLIENT_ID"] = input("  Client ID: ").strip()
        os.environ["PAYPAL_CLIENT_SECRET"] = getpass.getpass("  Secret (hidden): ").strip()
    return Settings()  # type: ignore[call-arg]


def approve(label: str, url: str | None) -> None:
    print(f"\n  >> Approve {label} in your browser (log in as the US personal account, pick Visa).")
    print(f"     {url}")
    if url:
        webbrowser.open(url)
    input("     Press Enter after approving... ")


def save() -> None:
    results["finished_at"] = datetime.now(timezone.utc).isoformat()
    RESULT_FILE.write_text(json.dumps(results, indent=2, default=str))


def step(path: str, name: str, fn):
    try:
        out = fn()
        results["paths"].setdefault(path, []).append({"step": name, "ok": True, "result": out})
        print(f"  [OK]   {name}: {out}")
        return out
    except PayPalError as e:
        results["paths"].setdefault(path, []).append(
            {"step": name, "ok": False, "status": e.status, "issue": e.issue, "debug_id": e.debug_id}
        )
        print(f"  [FAIL] {name}: {e}")
        raise


def main() -> int:
    s = load_settings()
    pp = PayPalClient(s)
    run = uuid.uuid4().hex[:6]
    cap = cap_for(HOLD)
    print(f"\nTrueUp sandbox run {run}. Hold {HOLD}, over-capture cap {cap}.")

    # --- create the three holds up front so the buyer approves them in one go ---
    print("\n1) Booking: creating three $60 holds (intent=AUTHORIZE)")
    jobs = {}
    for key, desc in [("path1", "Plumber visit - small change"), ("path2", "Plumber visit - big change"),
                      ("path3", "Plumber visit - cancelled")]:
        jobs[key] = step(key, "create_hold_order", lambda d=desc, k=key: create_authorize_order(
            pp, HOLD, d, f"trueup-{run}-{k}", s.return_url, s.cancel_url).model_dump())
    for key, j in jobs.items():
        print(f"     {key}: {j['approve_url']}")
        if j["approve_url"]:
            webbrowser.open(j["approve_url"])
    input("\n  >> 3 tabs opened. Approve all 3 as the US personal account, then press Enter... ")

    auths = {k: step(k, "authorize_hold", lambda o=j["id"]: authorize_order(pp, o).model_dump())
             for k, j in jobs.items()}

    # --- Path 1: small change, auto-capture within the cap ---
    print(f"\n2) Path 1 - small change: $60 visit + $6 washer = $66 (<= cap {cap})")
    new_total = HOLD + WASHER.total
    step("path1", "auto_capture_66", lambda: capture_authorization(
        pp, auths["path1"]["id"], new_total, final_capture=True,
        note="Visit $60 + washer $6", invoice_id=f"trueup-{run}-path1").model_dump())

    # --- Path 2: big change above the cap -> supplemental order ---
    big_total = HOLD + WASHER.total + CARTRIDGE.total
    extra = big_total - HOLD
    print(f"\n3) Path 2 - big change: total {big_total} > cap {cap}")
    print(f"   (owner approval happens here in the real app) -> supplemental order for {extra}")
    sup = step("path2", "create_supplemental_order", lambda: create_supplemental_order(
        pp, [WASHER, CARTRIDGE], f"Price change: +{extra} (washer, faucet cartridge)",
        f"trueup-{run}-path2-sup", s.return_url, s.cancel_url).model_dump())
    approve("the price change (+$51)", sup["approve_url"])
    step("path2", "capture_supplemental_51", lambda: capture_order(pp, sup["id"]).model_dump())
    step("path2", "capture_hold_60", lambda: capture_authorization(
        pp, auths["path2"]["id"], HOLD, final_capture=True, note="Visit $60",
        invoice_id=f"trueup-{run}-path2").model_dump())

    # --- Path 3: cancelled -> void ---
    print("\n4) Path 3 - cancelled: void the hold")
    step("path3", "void_hold", lambda: void_authorization(pp, auths["path3"]["id"]) or "voided")

    save()
    print("\nSummary")
    print(f"  Path 1  small change  charged {new_total} on one hold (auto, no new approval)")
    print(f"  Path 2  big change    charged {HOLD} hold + {extra} supplemental = {big_total}")
    print("  Path 3  cancelled     hold voided, customer paid 0.00")
    print(f"\nSaved {RESULT_FILE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PayPalError:
        save()
        print("\nStopped on a PayPal error. Result saved; send it to Claude.")
        sys.exit(1)
    except KeyboardInterrupt:
        save()
        sys.exit(130)
