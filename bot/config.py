"""Настройки из .env (описание полей — в .env.example)."""

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _split_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8-sig",  # Блокнот любит сохранять с BOM
        case_sensitive=False,
        extra="ignore",
        env_ignore_empty=True,
    )

    telegram_bot_token: str
    owner_telegram_id: int
    timezone: str = "Asia/Vladivostok"
    telegram_proxy: str = ""     # socks5://… / http://…, если api.telegram.org недоступен напрямую
    telegram_api_url: str = ""   # свой релей Bot API

    chat_base_url: str = ""
    chat_api_key: str = ""
    chat_model: str = ""
    cheap_model: str = ""
    chat_fallback_models: str = ""
    chat_disable_reasoning: bool = True
    price_per_1m_input_rub: float = 0.0
    price_per_1m_output_rub: float = 0.0
    monthly_budget_rub: float = 300.0

    chat_backup_base_url: str = ""
    chat_backup_api_key: str = ""
    chat_backup_model: str = ""

    erp_min_stage: str = Field(default="romance", pattern="^(friends|close_friends|romance)$")

    vision_model: str = "mistralai/mistral-small-3.2-24b-instruct"
    # последняя в списке — самая дешёвая, на неё переходим при 80% бюджета
    image_models: str = "bytedance/seedream-4,tongyi-mai/z-image"
    images_enabled: bool = True
    max_images_per_day: int = 2

    proactive_per_day: int = 4
    proactive_test_mode: bool = False

    data_dir: Path = PROJECT_ROOT / "data"
    persona_file: Path = PROJECT_ROOT / "persona.yaml"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def chat_fallback_models_list(self) -> list[str]:
        return _split_list(self.chat_fallback_models)

    @property
    def image_models_list(self) -> list[str]:
        return _split_list(self.image_models)


@lru_cache
def get_settings() -> Settings:
    return Settings()
