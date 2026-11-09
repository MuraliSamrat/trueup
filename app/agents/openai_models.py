"""OpenAI wiring: chat models for agents and speech-to-text for voice notes."""
from __future__ import annotations

from pathlib import Path

from app.config import Settings


def _key(settings: Settings) -> str:
    if settings.openai_api_key is None:
        raise RuntimeError("OPENAI_API_KEY is not set (add it to .env)")
    return settings.openai_api_key.get_secret_value()


def chat_models(settings: Settings):
    """(primary, fallback) LangChain chat models."""
    from langchain_openai import ChatOpenAI

    key = _key(settings)
    primary = ChatOpenAI(model=settings.openai_model, api_key=key, temperature=0, timeout=60)
    fallback = ChatOpenAI(model=settings.openai_fallback_model, api_key=key, temperature=0, timeout=60)
    return primary, fallback


def transcribe(settings: Settings, audio_path: str | Path) -> str:
    """Voice note -> text. Tries the configured model, then whisper-1."""
    from openai import OpenAI

    client = OpenAI(api_key=_key(settings))
    last_error: Exception | None = None
    for model in dict.fromkeys([settings.openai_transcribe_model, "whisper-1"]):
        try:
            with open(audio_path, "rb") as f:
                return client.audio.transcriptions.create(model=model, file=f).text
        except Exception as e:  # model retired / not enabled for this key
            last_error = e
    raise RuntimeError(f"Transcription failed: {last_error}")
