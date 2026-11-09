"""Low-level PayPal REST client: OAuth token caching, idempotency keys, typed errors.

Secrets (client secret, access token) are never logged or put into exceptions.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import httpx

from app.config import Settings

log = logging.getLogger("trueup.paypal")

TOKEN_REFRESH_MARGIN_S = 60


class PayPalError(Exception):
    """A non-2xx response from PayPal. Branch on `issue`, not on the message text."""

    def __init__(
        self,
        status: int,
        name: str | None = None,
        issue: str | None = None,
        description: str | None = None,
        debug_id: str | None = None,
        raw: Any = None,
    ) -> None:
        self.status = status
        self.name = name
        self.issue = issue
        self.description = description
        self.debug_id = debug_id
        self.raw = raw
        super().__init__(f"PayPal {status} {issue or name or 'ERROR'}: {description or ''} (debug_id={debug_id})")

    @classmethod
    def from_response(cls, resp: httpx.Response) -> PayPalError:
        try:
            body = resp.json()
        except ValueError:
            body = {"message": resp.text[:500]}
        details = body.get("details") or [{}]
        first = details[0] if details else {}
        return cls(
            status=resp.status_code,
            name=body.get("name") or body.get("error"),
            issue=first.get("issue") or body.get("error"),
            description=first.get("description") or body.get("message") or body.get("error_description"),
            debug_id=body.get("debug_id"),
            raw=body,
        )


class PayPalClient:
    def __init__(self, settings: Settings, http: httpx.Client | None = None) -> None:
        self._settings = settings
        self._http = http or httpx.Client(base_url=settings.paypal_base_url, timeout=30.0)
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # ---- auth -------------------------------------------------------------
    def _fetch_token(self) -> None:
        """POST /v1/oauth2/token (client_credentials)."""
        resp = self._http.post(
            "/v1/oauth2/token",
            auth=(self._settings.paypal_client_id, self._settings.paypal_client_secret.get_secret_value()),
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json"},
        )
        if resp.status_code != 200:
            raise PayPalError.from_response(resp)
        body = resp.json()
        self._token = body["access_token"]
        self._token_expires_at = time.monotonic() + int(body.get("expires_in", 0)) - TOKEN_REFRESH_MARGIN_S
        log.info("Fetched PayPal access token (expires_in=%ss)", body.get("expires_in"))

    def _get_token(self) -> str:
        if self._token is None or time.monotonic() >= self._token_expires_at:
            self._fetch_token()
        assert self._token is not None
        return self._token

    # ---- requests ---------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None = None,
        request_id: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Call a PayPal endpoint. Returns parsed JSON, or {} for an empty (204) body."""
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if method.upper() == "POST":
            headers["PayPal-Request-Id"] = request_id or str(uuid.uuid4())
        if extra_headers:
            headers.update(extra_headers)

        resp = self._send(method, path, json, headers)
        if resp.status_code == 401:  # token revoked/expired early: refresh once
            self._token = None
            resp = self._send(method, path, json, headers)

        if not resp.is_success:
            err = PayPalError.from_response(resp)
            log.warning("PayPal %s %s failed: %s %s debug_id=%s", method, path, err.status, err.issue, err.debug_id)
            raise err
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    def _send(self, method: str, path: str, json: dict[str, Any] | None, headers: dict[str, str]) -> httpx.Response:
        headers = {**headers, "Authorization": f"Bearer {self._get_token()}"}
        body = json if json is not None else ({} if method.upper() == "POST" else None)
        return self._http.request(method, path, json=body, headers=headers)

    def close(self) -> None:
        self._http.close()
