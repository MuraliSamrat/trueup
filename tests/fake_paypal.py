"""A small stateful fake of the PayPal endpoints TrueUp uses (for respx)."""
from __future__ import annotations

import itertools
import json
import threading
from decimal import Decimal

import httpx


class FakePayPal:
    def __init__(self, router) -> None:
        self.orders: dict[str, dict] = {}
        self.auths: dict[str, dict] = {}
        self.captures: list[dict] = []
        self.voided: list[str] = []
        self._ids = itertools.count(1)
        self._replies: dict[str, httpx.Response] = {}
        self._lock = threading.Lock()
        r = router
        r.post("/v2/checkout/orders").mock(side_effect=self._idem(self.create_order))
        r.get(url__regex=r"/v2/checkout/orders/(?P<oid>[A-Z0-9]+)$").mock(side_effect=self.get_order)
        r.post(url__regex=r"/v2/checkout/orders/(?P<oid>[A-Z0-9]+)/authorize").mock(side_effect=self._idem(self.authorize))
        r.post(url__regex=r"/v2/checkout/orders/(?P<oid>[A-Z0-9]+)/capture").mock(side_effect=self._idem(self.capture_order))
        r.post(url__regex=r"/v2/payments/authorizations/(?P<aid>[A-Z0-9]+)/capture").mock(side_effect=self._idem(self.capture_auth))
        r.post(url__regex=r"/v2/payments/authorizations/(?P<aid>[A-Z0-9]+)/void").mock(side_effect=self._idem(self.void))

    def _idem(self, handler):
        """Like real PayPal: a repeated PayPal-Request-Id returns the original response, no new action."""
        def wrapped(request, **kw):
            rid = request.headers.get("PayPal-Request-Id")
            with self._lock:
                if rid and rid in self._replies:
                    old = self._replies[rid]
                    return httpx.Response(old.status_code, content=old.content, headers=old.headers)
                resp = handler(request, **kw)
                if rid:
                    self._replies[rid] = resp
                return resp
        return wrapped

    def _id(self, p: str) -> str:
        return f"{p}{next(self._ids):04d}"

    # customer approves at PayPal (in real life: a browser login)
    def approve(self, order_id: str) -> None:
        self.orders[order_id]["status"] = "APPROVED"

    def create_order(self, request):
        b = json.loads(request.content)
        oid = self._id("ORD")
        pu = b["purchase_units"][0]
        self.orders[oid] = {"id": oid, "status": "PAYER_ACTION_REQUIRED", "intent": b["intent"],
                            "amount": pu["amount"]["value"], "custom_id": pu.get("custom_id"), "items": pu.get("items")}
        return httpx.Response(200, json={"id": oid, "status": "PAYER_ACTION_REQUIRED",
                                         "links": [{"rel": "payer-action", "href": f"https://sandbox.paypal/approve/{oid}"}]})

    def get_order(self, request, oid):
        o = self.orders[oid]
        return httpx.Response(200, json={"id": oid, "status": o["status"]})

    def authorize(self, request, oid):
        o = self.orders[oid]
        if o["status"] != "APPROVED":
            return httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY", "debug_id": "d",
                                             "details": [{"issue": "ORDER_NOT_APPROVED"}]})
        aid = self._id("AUTH")
        self.auths[aid] = {"amount": Decimal(o["amount"]), "captured": Decimal("0")}
        o["status"] = "COMPLETED"
        return httpx.Response(201, json={"id": oid, "status": "COMPLETED", "purchase_units": [{
            "custom_id": o["custom_id"], "payments": {"authorizations": [{
                "id": aid, "status": "CREATED", "amount": {"currency_code": "USD", "value": o["amount"]},
                "expiration_time": "2026-12-08T00:00:00Z"}]}}]})

    def capture_auth(self, request, aid):
        b = json.loads(request.content)
        a = self.auths[aid]
        amt = Decimal(b["amount"]["value"])
        cap = min(a["amount"] * Decimal("1.15"), a["amount"] + 75)
        if a["captured"] + amt > cap:
            return httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY", "debug_id": "d",
                                             "details": [{"issue": "MAX_CAPTURE_AMOUNT_EXCEEDED"}]})
        a["captured"] += amt
        c = {"id": self._id("CAP"), "status": "COMPLETED", "amount": b["amount"], "final_capture": b["final_capture"]}
        self.captures.append({"auth": aid, **c})
        return httpx.Response(201, json=c)

    def capture_order(self, request, oid):
        o = self.orders[oid]
        if o["status"] != "APPROVED":
            return httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY", "debug_id": "d",
                                             "details": [{"issue": "ORDER_NOT_APPROVED"}]})
        o["status"] = "COMPLETED"
        c = {"id": self._id("CAP"), "status": "COMPLETED", "amount": {"currency_code": "USD", "value": o["amount"]}}
        self.captures.append({"order": oid, **c})
        return httpx.Response(201, json={"id": oid, "status": "COMPLETED",
                                         "purchase_units": [{"payments": {"captures": [c]}}]})

    def void(self, request, aid):
        self.voided.append(aid)
        return httpx.Response(204)

    def charged(self) -> Decimal:
        return sum((Decimal(c["amount"]["value"]) for c in self.captures), Decimal("0"))
