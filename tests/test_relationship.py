"""Отношения. Уровень и время подставляются вручную."""

import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from bot.config import PROJECT_ROOT
from bot.dialogue import Dialogue
from bot.persona import load_persona
from bot.relationship import (
    KEY_ERP,
    KEY_FIXED,
    KEY_LEVEL,
    START_LEVEL,
    RelationshipTracker,
    stage_for,
)
from tests.helpers import ScriptedProvider, make_router, make_settings

TZ = ZoneInfo("Asia/Vladivostok")
NOW = datetime(2026, 10, 26, 15, 0, tzinfo=TZ)
PERSONA = load_persona(PROJECT_ROOT / "persona.yaml")


def make_tracker(store, *answers, now=NOW, **settings):
    provider = ScriptedProvider(*answers)
    return RelationshipTracker(store, make_router(store, provider), make_settings(**settings), now=lambda: now), provider


# ---------- Стадии ----------

def test_stage_thresholds_and_romance_needs_his_interest():
    assert stage_for(0, False) == "acquaintances"
    assert stage_for(29.9, False) == "acquaintances"
    assert stage_for(30, False) == "friends"
    assert stage_for(60, False) == "close_friends"
    assert stage_for(95, False) == "close_friends"   # без его интереса романтики нет
    assert stage_for(95, True) == "romance"


async def test_starts_as_acquaintances(store):
    tracker, _ = make_tracker(store)
    assert await tracker.level() == START_LEVEL
    assert await tracker.stage() == "acquaintances"


# ---------- Медленный рост ----------

async def test_delta_is_clamped_and_daily_gain_capped(store):
    tracker, _ = make_tracker(store)
    assert await tracker.apply(10, "очень тепло", False, NOW) == START_LEVEL + 3      # не больше +3
    assert await tracker.apply(3, "снова тепло", False, NOW + timedelta(hours=5)) == START_LEVEL + 3  # лимит дня
    assert await tracker.apply(3, "новый день", False, NOW + timedelta(days=1)) == START_LEVEL + 6
    assert await tracker.apply(-10, "грубость", False, NOW + timedelta(days=1)) == START_LEVEL + 3  # минус — без лимита


async def test_close_friends_take_weeks(store):
    tracker, _ = make_tracker(store)
    day = 0
    while await tracker.stage() != "close_friends":
        await tracker.apply(3, "идеальный день", False, NOW + timedelta(days=day))
        await tracker.apply(3, "ещё раз в тот же день", False, NOW + timedelta(days=day, hours=3))
        day += 1
    assert day >= 14   # даже при идеальном общении каждый день — минимум две недели


async def test_level_stays_in_bounds(store):
    tracker, _ = make_tracker(store)
    for i in range(10):
        await tracker.apply(-3, "ссора", False, NOW + timedelta(days=i))
    assert await tracker.level() == 0


async def test_fixed_stage_and_erp_setting(store):
    tracker, _ = make_tracker(store)
    assert not await tracker.erp_allowed()                  # по умолчанию ERP с «романтики»
    await store.set_setting(KEY_FIXED, "close_friends")
    assert await tracker.stage() == "close_friends"
    await store.set_setting(KEY_ERP, "close_friends")
    assert await tracker.erp_allowed()
    await store.set_setting(KEY_FIXED, "")                  # «авто»
    assert await tracker.stage() == "acquaintances"


# ---------- Оценка разговоров ----------

async def add_chat(store, count, start=NOW, mode="chat"):
    for i in range(count):
        await store.add_message("user" if i % 2 == 0 else "assistant", f"сообщение {i}", start + timedelta(minutes=i),
                                mode=mode)


async def test_evaluates_every_30_chat_messages(store):
    answer = json.dumps({"delta": 2, "reason": "делился планами, шутили", "romantic_interest": False})
    tracker, provider = make_tracker(store, answer)
    await store.set_setting("relationship:evaluated_at", NOW.isoformat())  # только что оценивали
    await add_chat(store, 29)
    await add_chat(store, 10, mode="rp")                    # ролевые не считаются
    assert not await tracker.maybe_evaluate()
    await add_chat(store, 1)
    assert await tracker.maybe_evaluate()
    assert await tracker.level() == START_LEVEL + 2
    history = await store.relationship_changes_since(NOW - timedelta(days=1))
    assert history[-1][2] == "делился планами, шутили"     # объяснение сохранено
    assert "сообщение" in provider.prompts[0][1]["content"]


async def test_evaluates_daily_with_few_messages(store):
    answer = json.dumps({"delta": 1, "reason": "мило поболтали", "romantic_interest": True})
    tracker, _ = make_tracker(store, answer)
    await store.set_setting("relationship:evaluated_at", (NOW - timedelta(days=1, hours=1)).isoformat())
    await add_chat(store, 6)
    assert await tracker.maybe_evaluate()
    assert await tracker.his_interest()                     # интерес запомнился


# ---------- Стадия меняет поведение ----------

async def system_prompt(store, tracker) -> str:
    dialogue = Dialogue(make_settings(), PERSONA, store, router=None, relationship=tracker)
    await store.add_message("user", "скучаю по тебе", NOW)
    return (await dialogue.build_messages(await store.recent_messages(5)))[0]["content"]


@pytest.mark.parametrize("level, interest, expected, erp", [
    (10, False, "Стадия: знакомые", False),
    (45, False, "Стадия: друзья", False),
    (70, False, "Стадия: близкие друзья", False),
    (90, True, "Стадия: романтика", True),
])
async def test_behaviour_changes_by_stage(store, level, interest, expected, erp):
    tracker, _ = make_tracker(store)
    await store.set_setting(KEY_LEVEL, str(level))
    if interest:
        await store.set_setting("relationship:his_interest", "1")
    prompt = await system_prompt(store, tracker)
    assert expected in prompt
    assert ("уместны: поддерживай их охотно" in prompt) == erp


async def test_close_stage_does_not_act_like_strangers(store):
    tracker, _ = make_tracker(store)
    await store.set_setting(KEY_FIXED, "romance")
    dialogue = Dialogue(make_settings(), PERSONA, store, router=None, relationship=tracker)
    await store.add_message("user", "покажи себя)", NOW)
    messages = await dialogue.build_messages(await store.recent_messages(5))
    assert "не торопит события" not in messages[0]["content"]   # «тормоз» из persona.yaml — только в начале
    assert "Ваши отношения сейчас: «романтика»" in messages[-1]["content"]  # напоминание в конце запроса
    assert "вы сблизились" in messages[-1]["content"]


async def test_early_stage_keeps_slow_development(store):
    tracker, _ = make_tracker(store)
    dialogue = Dialogue(make_settings(), PERSONA, store, router=None, relationship=tracker)
    await store.add_message("user", "привет", NOW)
    messages = await dialogue.build_messages(await store.recent_messages(5))
    assert "не торопит события" in messages[0]["content"]
    assert "вы сблизились" not in messages[-1]["content"]


async def test_status_is_human_without_numbers(store):
    tracker, _ = make_tracker(store)
    await store.set_setting(KEY_LEVEL, "57")
    text = await tracker.status_text()
    assert "друзья" in text and "ближе" in text            # почти «близкие друзья»
    assert not re.search(r"\d", text)
