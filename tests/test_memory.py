"""Долгая память. Нейросеть подменена: она возвращает заранее заданный JSON."""

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bot.config import PROJECT_ROOT
from bot.dialogue import Dialogue
from bot.memory.facts import build_match_query, extract_memory, parse_json
from bot.memory.updater import KEY_EXTRACTED, MemoryUpdater
from bot.persona import load_persona
from tests.helpers import ScriptedProvider, make_router, make_settings

TZ = ZoneInfo("Asia/Vladivostok")
NOW = datetime(2026, 10, 25, 20, 0, tzinfo=TZ)


async def add_dialogue(store, count, start=NOW):
    for i in range(count):
        await store.add_message("user" if i % 2 == 0 else "assistant", f"сообщение {i}", start + timedelta(minutes=i))


# ---------- Поиск ----------

def test_match_query_trims_endings_and_skips_stop_words():
    q = build_match_query("ну как там твоё собеседование прошло?")
    assert '"собеседован"*' in q
    assert '"как"*' not in q and '"там"*' not in q
    assert build_match_query("ну да") == ""


async def test_fts_finds_other_word_forms(store):
    await store.add_fact("события", "В среду у него собеседование в банке", 3, NOW)
    await store.add_fact("люди", "Сестру зовут Аня", 4, NOW)
    found = await store.search_facts(build_match_query("волнуюсь перед собеседованием"), limit=10)
    assert [f.text for f in found] == ["В среду у него собеседование в банке"]
    found = await store.search_facts(build_match_query("собеседованием"), limit=10, exclude_ids=[found[0].id])
    assert found == []


# ---------- Извлечение ----------

def test_parse_json_tolerates_wrapping():
    assert parse_json('вот:\n```json\n{"a": 1}\n```') == {"a": 1}


async def test_extract_memory_applies_everything(store):
    old_id = await store.add_fact("обо_мне", "Работает в офисе", 3, NOW)
    answer = json.dumps({
        "facts_new": [
            {"category": "обо_мне", "text": "Его зовут Влад", "importance": 5},
            {"category": "непонятно", "text": "Любит острое", "importance": 9},
            {"category": "обо_мне", "text": "Работает в офисе", "importance": 3},  # дубль — пропустить
        ],
        "facts_update": [{"id": old_id, "text": "Работает инженером в офисе", "importance": 4},
                         {"id": 999, "text": "чужой id", "importance": 1}],
        "her_life": ["Ника дорисовала заказ для кафе", {"text": "Подругу Ники зовут Катя", "category": "люди"}],
        "followups": [{"text": "спросить про собеседование", "due": "2026-10-28"},
                      {"text": "прошлое", "due": "2020-01-01 10:00"}],
    }, ensure_ascii=False)
    provider = ScriptedProvider(answer)
    router = make_router(store, provider)
    await add_dialogue(store, 4)
    stats = await extract_memory(router, store, await store.recent_messages(4), [], NOW)

    facts = {f.text: f for f in await store.all_facts()}
    assert stats.new_facts == 2 and stats.updated_facts == 1
    assert facts["Его зовут Влад"].importance == 5
    assert facts["Любит острое"].category == "обо_мне" and facts["Любит острое"].importance == 5
    assert "Работает инженером в офисе" in facts
    assert [e.text for e in await store.recent_her_events(5)] == ["Ника дорисовала заказ для кафе",
                                                                  "Подругу Ники зовут Катя"]
    followups = await store.open_followups()
    assert len(followups) == 1 and followups[0].due_at == datetime(2026, 10, 28, 19, 0, tzinfo=TZ)
    # Модель видит уже известные факты, чтобы не плодить дубли
    assert "Работает в офисе" in provider.prompts[0][1]["content"]


async def test_updater_waits_for_10_messages_and_skips_rp(store):
    provider = ScriptedProvider(json.dumps({"facts_new": [{"category": "обо_мне", "text": "Факт", "importance": 3}]}))
    router = make_router(store, provider)
    updater = MemoryUpdater(store, router, history_limit=20, now=lambda: NOW)

    await add_dialogue(store, 9)
    await updater.run()
    assert provider.prompts == []  # меньше 10 — ещё рано

    await store.add_message("user", "*мой персонаж достаёт меч*", NOW, mode="rp")
    await updater.run()
    assert len(provider.prompts) == 1
    assert "достаёт меч" not in provider.prompts[0][1]["content"]  # RP — не факты обо мне
    assert await store.get_setting(KEY_EXTRACTED) == str((await store.last_message()).id)


async def test_updater_summarizes_messages_outside_window(store):
    provider = ScriptedProvider("{}", "Вот обновлённая сводка:\n\nОн рассказал про работу, Ника — про кошку.")
    router = make_router(store, provider)
    updater = MemoryUpdater(store, router, history_limit=20, now=lambda: NOW)
    await add_dialogue(store, 32)  # 12 сообщений выпали из окна в 20
    await updater.run()
    summary = await store.latest_summary()
    assert summary.text == "Он рассказал про работу, Ника — про кошку."
    window = await store.recent_messages(20)
    assert summary.last_message_id == window[0].id - 1


# ---------- Память в запросе ----------

async def test_dialogue_includes_memory(store):
    await store.add_fact("обо_мне", "Его зовут Влад", 5, NOW)
    await store.add_fact("события", "В среду у него собеседование", 2, NOW)
    await store.add_her_event("Ника рассказала, что её подругу зовут Катя", NOW)
    await store.add_summary("Вчера болтали про аниме.", NOW, NOW, 0, NOW)
    await store.add_message("user", "блин, завтра собеседование, волнуюсь", NOW)

    dialogue = Dialogue(make_settings(), load_persona(PROJECT_ROOT / "persona.yaml"), store, router=None)
    messages = await dialogue.build_messages(await store.recent_messages(20))
    memory_block = messages[1]["content"]
    assert "Его зовут Влад" in memory_block            # важный факт — всегда
    assert "Вчера болтали про аниме" in memory_block   # сводка
    assert "подругу зовут Катя" in memory_block        # её жизнь
    assert "собеседование" not in memory_block         # неважный факт — только по теме…
    assert "В среду у него собеседование" in messages[-1]["content"]  # …и вот он, по теме


# ---------- Сброс и забывание ----------

async def test_forget_and_reset(store):
    fact_id = await store.add_fact("обо_мне", "Его зовут Влад", 5, NOW)
    await store.add_her_event("Ника нарисовала кота", NOW)
    await add_dialogue(store, 4)

    assert (await store.delete_fact(fact_id)).text == "Его зовут Влад"
    assert await store.delete_fact(fact_id) is None
    assert await store.search_facts(build_match_query("Влад"), limit=5) == []  # и из поиска тоже

    await store.add_fact("обо_мне", "Любит кофе", 3, NOW)
    await store.reset_conversation()
    assert await store.recent_messages(10) == [] and len(await store.all_facts()) == 1
    await store.reset_all()
    assert await store.all_facts() == [] and await store.recent_her_events(5) == []
