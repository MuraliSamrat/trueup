#!/usr/bin/env python3
"""
TrueUp - Day 1 PayPal sandbox verification.

Checks the payment moves TrueUp depends on:
  A. Authorize $60, then capture $69 (over-capture within the 115% / +$75 cap)
  B. Authorize $60, then capture $20 (not final) + $30 (multi / partial capture)
  C. Authorize $60, try to capture $120 (should be REJECTED: above the cap), then void

No installs needed: standard-library Python 3.8+ only.

Run:
  export PAYPAL_CLIENT_ID=...      (Windows PowerShell: $env:PAYPAL_CLIENT_ID="...")
  export PAYPAL_CLIENT_SECRET=...
  python3 day1_sandbox_test.py

You will be asked to open 3 approval links and approve each one while signed in as
your sandbox PERSONAL (buyer) account at www.sandbox.paypal.com.
Results are printed and saved to day1_results.json - send that file back.
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
import webbrowser
from datetime import datetime, timezone

API = "https://api-m.sandbox.paypal.com"
RESULTS = {"started_at": datetime.now(timezone.utc).isoformat(), "steps": []}


def call(method, path, token=None, body=None, basic=None, form=None):
    """Make a PayPal API call. Returns (http_status, parsed_json_or_text)."""
    headers = {"Accept": "application/json"}
    data = None
    if basic:
        headers["Authorization"] = "Basic " + base64.b64encode(basic.encode()).decode()
    if token:
        headers["Authorization"] = "Bearer " + token
        headers["PayPal-Request-Id"] = str(uuid.uuid4())  # idempotency key
    if form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = form.encode()
    elif body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    elif method == "POST":
        headers["Content-Type"] = "application/json"
        data = b"{}"
    req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode() or "{}"
            return r.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def record(name, status, payload, expect_ok=True):
    ok = 200 <= status < 300
    passed = ok if expect_ok else not ok
    issue = None
    if isinstance(payload, dict) and payload.get("details"):
        issue = payload["details"][0].get("issue")
    RESULTS["steps"].append({
        "step": name, "http": status, "passed": passed,
        "expected": "success" if expect_ok else "rejection",
        "issue": issue,
        "debug_id": payload.get("debug_id") if isinstance(payload, dict) else None,
        "response": payload,
    })
    mark = "PASS" if passed else "FAIL"
    extra = f"  issue={issue}" if issue else ""
    print(f"  [{mark}] {name}: HTTP {status}{extra}")
    return passed


def money(value):
    return {"currency_code": "USD", "value": f"{value:.2f}"}


def main():
    cid = os.environ.get("PAYPAL_CLIENT_ID") or input("Sandbox Client ID: ").strip()
    sec = os.environ.get("PAYPAL_CLIENT_SECRET") or input("Sandbox Secret: ").strip()

    print("\n1) Getting an access token...")
    st, tok = call("POST", "/v1/oauth2/token", basic=f"{cid}:{sec}",
                   form="grant_type=client_credentials")
    if not record("oauth_token", st, {k: v for k, v in tok.items() if k != "access_token"}
                  if isinstance(tok, dict) else tok):
        print("Could not get a token. Check the Client ID/Secret are from the SANDBOX tab.")
        save()
        sys.exit(1)
    token = tok["access_token"]

    print("\n2) Creating 3 orders with intent=AUTHORIZE ($60 each)...")
    orders = {}
    for label in ("A_overcapture", "B_partial", "C_over_cap_then_void"):
        body = {
            "intent": "AUTHORIZE",
            "purchase_units": [{
                "reference_id": label,
                "custom_id": "mandate-test-" + label,  # where the mandate hash will live
                "description": "TrueUp test visit",
                "amount": money(60),
            }],
            "payment_source": {"paypal": {"experience_context": {
                "user_action": "CONTINUE",
                "return_url": "https://example.com/return",
                "cancel_url": "https://example.com/cancel",
            }}},
        }
        st, o = call("POST", "/v2/checkout/orders", token, body)
        if not record(f"create_order_{label}", st, o):
            save()
            sys.exit(1)
        link = next((l["href"] for l in o.get("links", [])
                     if l["rel"] in ("payer-action", "approve")), None)
        orders[label] = {"id": o["id"], "approve": link}

    print("\n3) Approve each order as the BUYER.")
    print("   Open each link, log in with your sandbox PERSONAL account, click through to approve.")
    print("   After approving, PayPal sends you to example.com - that's expected, just close it.\n")
    for label, o in orders.items():
        print(f"   {label}:\n   {o['approve']}\n")
        try:
            webbrowser.open(o["approve"])  # opens each approval page in a new tab
        except Exception:
            pass
    input("   3 tabs opened. Approve all 3, then press Enter here... ")

    print("\n4) Authorizing the approved orders...")
    auths = {}
    for label, o in orders.items():
        st, a = call("POST", f"/v2/checkout/orders/{o['id']}/authorize", token)
        if record(f"authorize_{label}", st, a):
            auth = a["purchase_units"][0]["payments"]["authorizations"][0]
            auths[label] = auth["id"]
            RESULTS.setdefault("authorizations", {})[label] = {
                "id": auth["id"], "status": auth.get("status"),
                "expiration_time": auth.get("expiration_time"),
            }

    def capture(auth_id, value, final=True):
        body = {"amount": money(value), "final_capture": final}
        return call("POST", f"/v2/payments/authorizations/{auth_id}/capture", token, body)

    print("\n5) Test A - over-capture: $69 on a $60 authorization (115%)")
    if "A_overcapture" in auths:
        st, r = capture(auths["A_overcapture"], 69)
        RESULTS["overcapture_supported"] = record("A_capture_69_on_60", st, r)

    print("\n6) Test B - partial / multiple captures: $20 (not final), then $30 (final)")
    if "B_partial" in auths:
        st, r = capture(auths["B_partial"], 20, final=False)
        p1 = record("B_capture_20_not_final", st, r)
        st, r = capture(auths["B_partial"], 30, final=True)
        p2 = record("B_capture_30_final", st, r)
        RESULTS["partial_capture_supported"] = p1 and p2

    print("\n7) Test C - above the cap: $120 on $60 should be REJECTED, then void")
    if "C_over_cap_then_void" in auths:
        st, r = capture(auths["C_over_cap_then_void"], 120)
        RESULTS["cap_enforced"] = record("C_capture_120_rejected", st, r, expect_ok=False)
        st, r = call("POST", f"/v2/payments/authorizations/{auths['C_over_cap_then_void']}/void", token)
        RESULTS["void_supported"] = record("C_void", st, r)

    save()
    print("\nSummary")
    for k in ("overcapture_supported", "partial_capture_supported", "cap_enforced", "void_supported"):
        print(f"  {k}: {RESULTS.get(k)}")
    print("\nSaved day1_results.json - send it back to Claude.")


def save():
    RESULTS["finished_at"] = datetime.now(timezone.utc).isoformat()
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "day1_results.json")
    with open(out, "w") as f:
        json.dump(RESULTS, f, indent=2)


if __name__ == "__main__":
    main()
