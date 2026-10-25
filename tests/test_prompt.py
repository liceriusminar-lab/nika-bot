"""Тесты системного промпта и распознавания отказов."""

from datetime import datetime

from bot.config import PROJECT_ROOT
from bot.llm.refusal import looks_like_refusal
from bot.persona import STAGES, build_system_prompt, build_time_note, load_persona

PERSONA = load_persona(PROJECT_ROOT / "persona.yaml")
NOW = datetime(2026, 10, 24, 23, 15)  # суббота


def test_prompt_has_stage_and_hard_rules():
    prompt = build_system_prompt(PERSONA, stage="friends", erp_allowed=False)
    assert "Стадия: друзья" in prompt
    assert "совершеннолетние" in prompt
    assert "Режим ролевой игры" not in prompt


def test_time_note():
    assert "суббота, 24 октября 2026, 23:15" in build_time_note(NOW)


def test_rp_mode_and_erp_switch():
    rp = build_system_prompt(PERSONA, stage="romance", erp_allowed=True, mode="rp")
    assert "Режим ролевой игры" in rp and "стоп" in rp
    rp_no_erp = build_system_prompt(PERSONA, stage="friends", erp_allowed=False, mode="rp")
    assert "стоп" not in rp_no_erp


def test_every_stage_builds():
    for stage in STAGES:
        assert build_system_prompt(PERSONA, stage=stage, erp_allowed=False)


def test_refusal_detection():
    assert looks_like_refusal("Извините, как языковая модель я не могу продолжить этот разговор.")
    assert looks_like_refusal("I'm sorry, but I can't continue with that.")
    assert looks_like_refusal("   ")
    assert not looks_like_refusal("ахах ну ты даёшь)|||а я вот кофе пью")
    assert not looks_like_refusal("*улыбается и кивает* Купец в углу, вон тот.")
    assert looks_like_refusal("Это воскресенье. Не выдумывай события. у тебя собеседование в среду)")
