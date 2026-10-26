"""Конфиг, персона и фильтр «только владелец»."""

from types import SimpleNamespace

import pytest

from bot.config import PROJECT_ROOT, Settings
from bot.handlers.owner_only import OwnerOnlyMiddleware
from bot.persona import PersonaError, load_persona

OWNER_ID = 111


def make_settings(**overrides) -> Settings:
    # _env_file=None — не читаем настоящий .env, чтобы тесты не зависели от него
    values = {"telegram_bot_token": "123:abc", "owner_telegram_id": OWNER_ID} | overrides
    return Settings(_env_file=None, **values)


def test_settings_lists_and_timezone():
    s = make_settings(chat_fallback_models="a, b,,c", timezone="Asia/Vladivostok")
    assert s.chat_fallback_models_list == ["a", "b", "c"]
    assert s.image_models_list == ["bytedance/seedream-4", "tongyi-mai/z-image"]
    assert str(s.tz) == "Asia/Vladivostok"


def test_settings_rejects_bad_erp_stage():
    with pytest.raises(ValueError):
        make_settings(erp_min_stage="whatever")


def test_real_persona_loads():
    persona = load_persona(PROJECT_ROOT / "persona.yaml")
    assert persona["name"] == "Ника"
    assert persona["age"] >= 18
    # Строки с двоеточием внутри должны остаться строками, а не превратиться в словари
    assert all(isinstance(item, str) for item in persona["interests"])


def test_persona_must_be_adult(tmp_path):
    bad = tmp_path / "persona.yaml"
    bad.write_text(
        'name: "X"\nage: 17\npersonality: a\nspeech_style: b\nappearance_prompt: c\n',
        encoding="utf-8",
    )
    with pytest.raises(PersonaError):
        load_persona(bad)


async def _run_middleware(user_id: int | None) -> bool:
    """Возвращает True, если событие дошло до обработчика."""
    reached = False

    async def handler(event, data):
        nonlocal reached
        reached = True

    user = SimpleNamespace(id=user_id) if user_id is not None else None
    await OwnerOnlyMiddleware(OWNER_ID)(handler, object(), {"event_from_user": user})
    return reached


async def test_owner_passes():
    assert await _run_middleware(OWNER_ID) is True


async def test_stranger_ignored():
    assert await _run_middleware(222) is False


async def test_event_without_user_ignored():
    assert await _run_middleware(None) is False
