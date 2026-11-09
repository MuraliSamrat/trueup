import httpx
import pytest
import respx

from app.config import Settings
from app.paypal.client import PayPalClient

BASE = "https://api-m.sandbox.paypal.com"
SECRET = "super-secret-value"
TOKEN = "access-token-xyz"


@pytest.fixture
def settings() -> Settings:
    return Settings(paypal_client_id="client-id", paypal_client_secret=SECRET, paypal_env="sandbox", _env_file=None)


@pytest.fixture
def mock_api():
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        router.post("/v1/oauth2/token").mock(
            return_value=httpx.Response(200, json={"access_token": TOKEN, "expires_in": 32400})
        )
        yield router


@pytest.fixture
def client(settings, mock_api) -> PayPalClient:
    return PayPalClient(settings)
