"""Application settings, read from environment variables (and a local .env file)."""
import os
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PAYPAL_BASE_URLS = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live": "https://api-m.paypal.com",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    paypal_client_id: str
    paypal_client_secret: SecretStr  # SecretStr keeps it out of repr() and logs
    paypal_env: Literal["sandbox", "live"] = "sandbox"

    # OpenAI (bill agent, voice notes, photos). Model names are configurable so a
    # renamed/retired model never needs a code change.
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4.1-mini"
    openai_fallback_model: str = "gpt-4o-mini"
    openai_transcribe_model: str = "gpt-4o-mini-transcribe"

    # Public address of the web app (PayPal sends the buyer back here after approving).
    # Locally http://127.0.0.1:8000; on Render https://<your-app>.onrender.com
    public_base_url: str = "http://127.0.0.1:8000"

    # PayPal webhook id (Developer Dashboard -> your app -> Webhooks). Needed to verify webhook signatures.
    paypal_webhook_id: str | None = None

    # Password for the technician and owner pages (HTTP basic auth, user "owner" or "tech").
    staff_password: SecretStr = SecretStr("trueup-demo")

    # Demo mode: technician reports are accepted before the booked slot, and an "ASAP" slot is offered,
    # so the whole flow can be shown in a few minutes. Off in a real deployment.
    demo_mode: bool = True

    @model_validator(mode="after")
    def _render_url(self) -> "Settings":
        # On Render, RENDER_EXTERNAL_URL is set automatically (https://<app>.onrender.com).
        render_url = os.environ.get("RENDER_EXTERNAL_URL")
        if render_url and self.public_base_url.startswith("http://127.0.0.1"):
            self.public_base_url = render_url
        return self

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings,
                                   file_secret_settings):
        # The project's .env wins over machine-wide environment variables, so an old key saved in
        # Windows settings can't silently override it. On Render there is no .env: env vars apply.
        return init_settings, dotenv_settings, env_settings, file_secret_settings

    @property
    def return_url(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/paypal/return"

    @property
    def cancel_url(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/paypal/cancel"

    @property
    def paypal_base_url(self) -> str:
        return PAYPAL_BASE_URLS[self.paypal_env]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
