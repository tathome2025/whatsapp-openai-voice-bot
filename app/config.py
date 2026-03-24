from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_graph_version: str = "v23.0"

    openai_api_key: str = ""
    openai_transcribe_model: str = "gpt-4o-mini-transcribe"
    openai_response_model: str = "gpt-4.1-mini"
    openai_tts_model: str = "gpt-4o-mini-tts"
    openai_tts_voice: str = "alloy"
    openai_tts_format: str = "opus"
    openai_tts_voices: str = "alloy,aria,verse"
    openai_default_language: str = "zh-HK"
    openai_languages: str = "zh-HK,zh-TW,zh-CN,en,ja,ko"
    openai_system_prompt: str = (
        "You are a concise and helpful WhatsApp voice assistant. "
        "Reply in Traditional Chinese unless the user explicitly uses another language."
    )
    voice_store_path: str = "/tmp/voice_preferences.json"
    language_store_path: str = "/tmp/language_preferences.json"
    db_path: str = "/tmp/wa_voice_bot.sqlite3"
    admin_session_secret: str = ""
    admin_session_hours: int = 12
    admin_bootstrap_email: str = ""
    admin_bootstrap_password: str = ""

    max_reply_chars: int = 800

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("max_reply_chars", mode="before")
    @classmethod
    def normalize_max_reply_chars(cls, value: object) -> int:
        if value in (None, ""):
            return 800
        return max(80, int(value))

    @field_validator("admin_session_hours", mode="before")
    @classmethod
    def normalize_admin_session_hours(cls, value: object) -> int:
        if value in (None, ""):
            return 12
        return max(1, int(value))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
