"""Роутер моделей, база, «человечность» и сборка запроса.

Настоящие нейросети здесь не вызываются — вместо них подставные провайдеры.
"""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bot.config import PROJECT_ROOT
from bot.dialogue import CRISIS_NOTE, Dialogue, looks_like_crisis
from bot.humanize import Debouncer, clean_text, split_parts
from bot.llm.base import LLMError, Prices
from bot.llm.router import BACKUP_RECHECK_SECONDS
from bot.persona import load_persona
from tests.helpers import Clock, FakeProvider, make_router, make_settings

TZ = ZoneInfo("Asia/Vladivostok")


# ---------- Роутер ----------

async def test_router_uses_main_model_and_records_cost(store):
    primary = FakeProvider({"main": "привет)"})
    router = make_router(store, primary)
    router.prices = {"main": Prices(input=10, output=40, cached_input=1)}
    result = await router.chat([{"role": "user", "content": "привет"}])
    assert result.text == "привет)" and primary.calls == ["main"]
    # 500 обычных ток. по 10 ₽ + 500 из кэша по 1 ₽ + 100 выходных по 40 ₽ за млн
    assert await router.month_spent() == pytest.approx((500 * 10 + 500 * 1 + 100 * 40) / 1e6)


async def test_router_skips_refusal_to_fallback(store):
    primary = FakeProvider({"main": "Извините, как языковая модель я не могу продолжить.", "fb1": "ахах ну давай)"})
    router = make_router(store, primary)
    result = await router.chat([{"role": "user", "content": "..."}])
    assert result.text == "ахах ну давай)"
    assert primary.calls == ["main", "fb1"]


async def test_router_service_uses_cheap_model_without_refusal_check(store):
    primary = FakeProvider({"cheap": "Чем могу помочь?"})
    router = make_router(store, primary)
    result = await router.chat([], purpose="service")
    assert result.model == "cheap"


async def test_router_switches_to_backup_and_back(store):
    notes = []
    clock = Clock()
    down = LLMError("timeout", kind="network")
    primary = FakeProvider({"main": down, "fb1": down, "fb2": down}, alive=False)
    backup = FakeProvider({"reserve": "я тут)"})
    router = make_router(store, primary, backup=backup, notes=notes, clock=clock,
                         chat_backup_model="reserve")

    result = await router.chat([])
    assert result.text == "я тут)" and router.on_backup
    assert primary.calls == ["main", "fb1"]  # после 2 сбоев подряд — на резерв
    assert len(notes) == 1

    # Через 10 минут основной ожил — возвращаемся
    primary.alive = True
    primary.answers = {"main": "снова я)"}
    clock.t += BACKUP_RECHECK_SECONDS + 1
    result = await router.chat([])
    assert result.text == "снова я)" and not router.on_backup
    assert len(notes) == 2


async def test_router_no_money_without_backup_notifies_once(store):
    notes = []
    broke = LLMError("402", kind="no_money")
    primary = FakeProvider({"main": broke, "fb1": broke, "fb2": broke})
    router = make_router(store, primary, notes=notes)
    for _ in range(2):
        with pytest.raises(LLMError):
            await router.chat([])
    assert len(notes) == 1
    assert primary.calls == ["main", "main"]  # другие модели того же агрегатора не мучаем


async def test_router_budget_exceeded_switches_to_cheap(store):
    notes = []
    primary = FakeProvider({})
    router = make_router(store, primary, notes=notes, monthly_budget_rub=1)
    await store.add_usage(router.today().isoformat(), "main", 0, 0, 0, cost_rub=1.5)
    result = await router.chat([])
    assert result.model == "cheap"
    assert any("бюджет" in n for n in notes)


# ---------- База ----------

async def test_store_messages_order_and_settings(store):
    now = datetime(2026, 10, 25, 12, 0, tzinfo=TZ)
    for i in range(5):
        await store.add_message("user" if i % 2 == 0 else "assistant", f"m{i}", now)
    recent = await store.recent_messages(3)
    assert [m.content for m in recent] == ["m2", "m3", "m4"]
    assert recent[0].created_at == now
    await store.set_setting("x", "1")
    await store.set_setting("x", "2")
    assert await store.get_setting("x") == "2"
    assert await store.get_setting("nope", "def") == "def"


# ---------- Человечность ----------

def test_clean_text_removes_markdown_and_assistant_phrases():
    raw = "Ника: **привет**)\n- как ты?\n## заголовок\nЧем могу помочь?"
    assert clean_text(raw) == "привет)\nкак ты?\nзаголовок"


def test_clean_text_keeps_rp_actions():
    raw = "*улыбается* Ну привет, путник.\n*ставит кружку на стойку*"
    assert clean_text(raw) == raw


def test_split_parts():
    assert split_parts("а ||| б|||в") == ["а", "б", "в"]
    assert split_parts("первый абзац\n\nвторой\nс переносом") == ["первый абзац", "второй\nс переносом"]
    assert split_parts("1|||2|||3|||4") == ["1", "2", "3\n4"]
    assert split_parts("*действие* |||текст", mode="rp") == ["*действие* \n\nтекст"]
    long = ("слово " * 1000).strip()
    assert all(len(p) <= 4000 for p in split_parts(long))


async def test_debouncer_answers_once_for_burst():
    calls = []

    async def action():
        calls.append(1)

    d = Debouncer(action, delay=0.05)
    for _ in range(3):
        d.poke()
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.15)
    assert calls == [1]


# ---------- Сборка запроса ----------

def test_crisis_detection():
    assert looks_like_crisis("иногда думаю, что всем было бы проще без меня")
    assert looks_like_crisis("я не хочу больше жить")
    assert not looks_like_crisis("капец как устал, хочу спать")


async def test_dialogue_merges_messages_and_adds_notes(store):
    settings = make_settings()
    persona = load_persona(PROJECT_ROOT / "persona.yaml")
    dialogue = Dialogue(settings, persona, store, router=None)
    now = datetime.now(TZ)
    await store.add_message("user", "привет", now)
    await store.add_message("user", "лучше бы меня не было", now)
    messages = await dialogue.build_messages(await store.recent_messages(20))

    assert messages[0]["role"] == "system"
    assert len(messages) == 2  # склеенные сообщения + подсказка в том же сообщении
    assert messages[1]["role"] == "user"
    assert messages[1]["content"].startswith("привет\nлучше бы меня не было\n\n[подсказка для Ники")
    assert CRISIS_NOTE in messages[1]["content"] and "Сейчас" in messages[1]["content"]


async def test_dialogue_note_goes_as_user_when_no_user_message(store):
    persona = load_persona(PROJECT_ROOT / "persona.yaml")
    dialogue = Dialogue(make_settings(), persona, store, router=None)
    messages = await dialogue.build_messages([], extra_note="поздоровайся")
    assert messages[-1]["role"] == "user"
    assert "поздоровайся" in messages[-1]["content"]
