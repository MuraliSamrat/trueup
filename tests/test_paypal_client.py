import logging

import httpx
import pytest

from app.paypal.client import PayPalClient, PayPalError
from tests.conftest import SECRET, TOKEN


def test_token_fetched_once_and_cached(client, mock_api):
    mock_api.get("/v2/checkout/orders/A").mock(return_value=httpx.Response(200, json={"id": "A"}))
    client.request("GET", "/v2/checkout/orders/A")
    client.request("GET", "/v2/checkout/orders/A")
    assert mock_api.routes[0].call_count == 1  # oauth route
    sent = mock_api.calls.last.request
    assert sent.headers["Authorization"] == f"Bearer {TOKEN}"


def test_token_refreshed_after_expiry(settings, mock_api, monkeypatch):
    mock_api.get("/x").mock(return_value=httpx.Response(200, json={}))
    c = PayPalClient(settings)
    c.request("GET", "/x")
    c._token_expires_at = 0  # simulate expiry
    c.request("GET", "/x")
    assert mock_api.routes[0].call_count == 2


def test_401_triggers_one_refresh(client, mock_api):
    route = mock_api.get("/y").mock(
        side_effect=[httpx.Response(401, json={"error": "invalid_token"}), httpx.Response(200, json={"ok": True})]
    )
    assert client.request("GET", "/y") == {"ok": True}
    assert route.call_count == 2
    assert mock_api.routes[0].call_count == 2


def test_post_has_request_id_and_custom_one_is_kept(client, mock_api):
    route = mock_api.post("/z").mock(return_value=httpx.Response(201, json={}))
    client.request("POST", "/z", {"a": 1})
    assert route.calls.last.request.headers["PayPal-Request-Id"]
    client.request("POST", "/z", {"a": 1}, request_id="fixed-id")
    assert route.calls.last.request.headers["PayPal-Request-Id"] == "fixed-id"


def test_get_has_no_request_id(client, mock_api):
    route = mock_api.get("/g").mock(return_value=httpx.Response(200, json={}))
    client.request("GET", "/g")
    assert "PayPal-Request-Id" not in route.calls.last.request.headers


def test_204_returns_empty_dict(client, mock_api):
    mock_api.post("/v").mock(return_value=httpx.Response(204))
    assert client.request("POST", "/v") == {}


def test_422_becomes_typed_error(client, mock_api):
    mock_api.post("/cap").mock(
        return_value=httpx.Response(
            422,
            json={
                "name": "UNPROCESSABLE_ENTITY",
                "details": [{"issue": "MAX_CAPTURE_AMOUNT_EXCEEDED", "description": "Capture amount exceeds allowable limit."}],
                "debug_id": "dbg123",
            },
        )
    )
    with pytest.raises(PayPalError) as exc:
        client.request("POST", "/cap", {})
    e = exc.value
    assert (e.status, e.issue, e.debug_id) == (422, "MAX_CAPTURE_AMOUNT_EXCEEDED", "dbg123")


def test_bad_credentials_raise(settings):
    import respx

    with respx.mock(base_url="https://api-m.sandbox.paypal.com") as r:
        r.post("/v1/oauth2/token").mock(
            return_value=httpx.Response(401, json={"error": "invalid_client", "error_description": "Client Authentication failed"})
        )
        with pytest.raises(PayPalError) as exc:
            PayPalClient(settings).request("GET", "/anything")
    assert exc.value.issue == "invalid_client"


def test_secrets_never_in_logs_or_errors(client, mock_api, caplog, settings):
    caplog.set_level(logging.DEBUG)
    mock_api.post("/fail").mock(return_value=httpx.Response(400, json={"name": "INVALID_REQUEST", "debug_id": "d"}))
    with pytest.raises(PayPalError) as exc:
        client.request("POST", "/fail", {})
    text = caplog.text + str(exc.value) + repr(settings)
    assert SECRET not in text
    assert TOKEN not in text
