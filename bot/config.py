"""Настройки бота.

Все значения читаются из файла `.env` в корне проекта (или из переменных окружения).
Сам `.env` в git не попадает — там лежат секретные ключи. Образец со всеми
полями и пояснениями — `.env.example`.
"""

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Корень проекта: папка, где лежат bot/, persona.yaml, .env
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _split_list(value: str) -> list[str]:
    """'a, b,c' -> ['a', 'b', 'c']. Пустая строка -> пустой список."""
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        # utf-8-sig — понимает и файл, сохранённый Блокнотом с меткой BOM в начале
        env_file_encoding="utf-8-sig",
        # Имена в .env пишутся БОЛЬШИМИ буквами, а здесь — маленькими; так можно
        case_sensitive=False,
        # Лишние строки в .env не считаются ошибкой
        extra="ignore",
        # Пустое значение (PRICE_PER_1M_INPUT_RUB=) = "не задано", берётся значение по умолчанию
        env_ignore_empty=True,
    )

    # --- Telegram ---
    # Эти два поля обязательные: без них бот не запустится
    telegram_bot_token: str
    owner_telegram_id: int
    timezone: str = "Asia/Vladivostok"
    # Если API Telegram с сервера недоступен напрямую (бывает на российских хостингах):
    # прокси вида socks5://user:pass@host:1080 или http://host:3128
    telegram_proxy: str = ""
    # …или свой «релей» Bot API за рубежом, например https://tg-relay.example.com
    telegram_api_url: str = ""

    # --- Разговор: OpenAI-совместимый провайдер ---
    chat_base_url: str = ""
    chat_api_key: str = ""
    chat_model: str = ""
    cheap_model: str = ""
    chat_fallback_models: str = ""  # через запятую, см. свойство ниже
    # Отключать «размышления» моделей. false — если провайдер ругается на параметр reasoning
    chat_disable_reasoning: bool = True
    price_per_1m_input_rub: float = 0.0
    price_per_1m_output_rub: float = 0.0
    monthly_budget_rub: float = 300.0

    # --- Резервный провайдер разговора ---
    chat_backup_base_url: str = ""
    chat_backup_api_key: str = ""
    chat_backup_model: str = ""

    erp_min_stage: str = Field(default="romance", pattern="^(friends|close_friends|romance)$")

    # --- Картинки: тоже через агрегатор ---
    # Модель, которая понимает картинки — описывает мои фото
    vision_model: str = "mistralai/mistral-small-3.2-24b-instruct"
    # Модели рисования через запятую: первая — основная, последняя — самая дешёвая (при 80% бюджета)
    image_models: str = "bytedance/seedream-4,tongyi-mai/z-image"

    images_enabled: bool = True
    max_images_per_day: int = 2
    proactive_per_day: int = 4
    # Тестовый режим «пишет первой»: проверка каждую минуту, пороги в минутах вместо часов
    proactive_test_mode: bool = False

    # --- Пути (обычно менять не нужно) ---
    data_dir: Path = PROJECT_ROOT / "data"
    persona_file: Path = PROJECT_ROOT / "persona.yaml"

    @property
    def tz(self) -> ZoneInfo:
        """Часовой пояс как объект — для работы со временем."""
        return ZoneInfo(self.timezone)

    @property
    def chat_fallback_models_list(self) -> list[str]:
        return _split_list(self.chat_fallback_models)

    @property
    def image_models_list(self) -> list[str]:
        return _split_list(self.image_models)


@lru_cache
def get_settings() -> Settings:
    """Загружает настройки один раз и дальше отдаёт тот же объект."""
    return Settings()
