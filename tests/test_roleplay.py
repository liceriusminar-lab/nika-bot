"""Ролевые игры. Нейросеть подменена сценарием ответов."""

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bot.config import PROJECT_ROOT
from bot.dialogue import Dialogue
from bot.humanize import split_parts
from bot.memory.updater import MemoryUpdater
from bot.persona import load_persona
from bot.roleplay import RoleplayManager, detect_entry, is_ooc_text, is_stop
from tests.helpers import ScriptedProvider, make_router, make_settings

TZ = ZoneInfo("Asia/Vladivostok")
NOW = datetime(2026, 10, 26, 20, 0, tzinfo=TZ)
PERSONA = load_persona(PROJECT_ROOT / "persona.yaml")

READY = json.dumps({"ready": True, "title": "Таверна на окраине", "setting": "фэнтези, вечер, таверна",
                    "characters": "его персонаж: наёмник Рейн; персонаж Ники: трактирщица Лира",
                    "opening": "наёмник заходит в таверну в дождь"}, ensure_ascii=False)
NOT_READY = json.dumps({"ready": False})


def make_game(store, *answers):
    provider = ScriptedProvider(*answers)
    router = make_router(store, provider)
    roleplay = RoleplayManager(store, router, now=lambda: NOW)
    dialogue = Dialogue(make_settings(), PERSONA, store, router, roleplay=roleplay)
    return dialogue, roleplay, provider


async def say(store, text, mode="chat", scene_id=None):
    await store.add_message("user", text, NOW, mode=mode, scene_id=scene_id)


# ---------- Распознавание ----------

def test_detection():
    assert detect_entry("го поиграем в ролевку?") == "new"
    assert detect_entry("давай в рп") == "new"
    assert detect_entry("давай сыграем в ролевую игру") == "new"
    assert detect_entry("давай продолжим нашу историю") == "continue"
    assert detect_entry("го в кино") is None
    assert is_stop("стоп") and is_stop("Стоп!") and is_stop("/stop")
    assert not is_stop("стоп, а ты видела это?")
    assert is_ooc_text("((ахах класс))") and not is_ooc_text("*улыбается* ((шёпотом))")


# ---------- Полный цикл ----------

async def test_full_scene_cycle(store):
    dialogue, roleplay, provider = make_game(
        store,
        NOT_READY, "го) могу предложить фэнтези-таверну или киберпанк, что больше хочется?",
        READY, "*Лира протирает кружку и поднимает взгляд на промокшего путника* Поздновато для гостей.",
        "*ставит кружку на стойку* Монета чистая. Садись к огню.",
        "ахах спасибо) мне самой она нравится",
        "*Лира кивает, будто ничего не было* Так что, наёмник, работу ищешь?",
    )
    # 1. Договариваемся
    await roleplay.start_setup()
    await say(store, "го поиграем в ролевку?")
    reply = await dialogue.generate_reply()
    assert reply.mode == "chat" and (await roleplay.get()).state == "setup"
    assert "договариваетесь о текстовой ролевой игре" in provider.prompts[1][-1]["content"]

    # 2. Договорились — сцена начинается, ответ ролевой и одним сообщением
    await say(store, "давай таверну, я наёмник. начинай!")
    reply = await dialogue.generate_reply()
    rp = await roleplay.get()
    assert rp.state == "scene" and rp.scene.title == "Таверна на окраине"
    assert reply.mode == "rp" and len(split_parts(reply.text + "|||ещё", reply.mode)) == 1
    assert "Режим ролевой игры" in provider.prompts[3][0]["content"]
    assert "трактирщица Лира" in provider.prompts[3][2]["content"]   # блок сцены

    # 3. Ход в сцене
    await say(store, "*кладу монету на стойку* пиво", mode="rp", scene_id=rp.scene.id)
    reply = await dialogue.generate_reply()
    assert reply.mode == "rp"
    last = await store.last_message()
    assert last.mode == "rp"

    # 4. ((вне роли)) — отвечает как она сама, сцена не прерывается
    await say(store, "((ахах трактирщица огонь))")
    reply = await dialogue.generate_reply()
    assert reply.mode == "chat" and (await roleplay.get()).state == "scene"
    assert "Он вышел из роли" in provider.prompts[5][-1]["content"]

    # 5. Снова в роли
    await say(store, "*сажусь у огня*", mode="rp", scene_id=rp.scene.id)
    assert (await dialogue.generate_reply()).mode == "rp"


async def test_ooc_back_stop_resume_and_end(store):
    end_answer = json.dumps({"summary": "Наёмник Рейн нанялся охранять караван.",
                             "preference": "Любит играть с Никой в фэнтези-приключения"}, ensure_ascii=False)
    dialogue, roleplay, provider = make_game(store, "ок, вышла из роли) что такое?",
                                             "*Лира вздыхает* На чём мы остановились…", end_answer,
                                             "было весело)")
    scene = await roleplay.create_scene("Таверна", "фэнтези", "Рейн; Лира")
    await say(store, "*захожу*", mode="rp", scene_id=scene.id)

    await roleplay.ooc()
    assert (await dialogue.generate_reply("Он вышел из роли, чтобы поболтать.")).mode == "chat"
    await roleplay.back_to_scene()
    assert (await dialogue.generate_reply("возвращаемся")).mode == "rp"

    # «стоп» — пауза, вне игры
    await roleplay.stop()
    assert (await roleplay.get()).state == ""
    assert (await store.get_scene(scene.id)).status == "paused"
    assert (await roleplay.last_unfinished_scene()).id == scene.id

    # продолжить старую сцену
    await roleplay.resume(scene.id)
    rp = await roleplay.get()
    assert rp.state == "scene" and (await store.get_scene(scene.id)).status == "active"

    # конец: итог сцены и предпочтение в памяти
    ended = await roleplay.end()
    assert ended.status == "ended" and ended.summary == "Наёмник Рейн нанялся охранять караван."
    assert [f.text for f in await store.all_facts()] == ["Любит играть с Никой в фэнтези-приключения"]
    assert (await roleplay.get()).state == ""


async def test_new_scene_pauses_previous(store):
    _, roleplay, _ = make_game(store)
    first = await roleplay.create_scene("Первая", "", "")
    await roleplay.create_scene("Вторая", "", "")
    assert (await store.get_scene(first.id)).status == "paused"


async def test_proactive_during_scene_is_out_of_role(store):
    dialogue, roleplay, provider = make_game(store, "слушай, я придумала, что будет дальше в нашей истории)")
    scene = await roleplay.create_scene("Таверна", "", "")
    await store.add_message("assistant", "*Лира улыбается*", NOW, mode="rp", scene_id=scene.id)
    reply = await dialogue.generate_reply("Сейчас ты пишешь ему первой", ooc=True)
    assert reply.mode == "chat"
    assert "Режим ролевой игры" not in provider.prompts[0][0]["content"]
    assert "недоигранная ролевая сцена «Таверна»" in provider.prompts[0][-1]["content"]
    assert (await store.last_message()).mode == "chat"


# ---------- Память ----------

async def test_character_facts_do_not_reach_memory(store):
    provider = ScriptedProvider("{}")
    router = make_router(store, provider)
    updater = MemoryUpdater(store, router, history_limit=20, now=lambda: NOW)
    await say(store, "меня зовут Влад, я инженер")
    for i in range(10):
        await say(store, f"*мой персонаж Рейн, наёмник с севера, достаёт меч {i}*", mode="rp", scene_id=1)
    await updater.run(force=True)
    sent = provider.prompts[0][1]["content"]
    assert "инженер" in sent and "Рейн" not in sent


async def test_long_scene_gets_summarized(store):
    provider = ScriptedProvider("Рейн и Лира подружились, караван ушёл на север.")
    router = make_router(store, provider)
    roleplay = RoleplayManager(store, router, now=lambda: NOW)
    scene = await roleplay.create_scene("Таверна", "", "")
    for i in range(45):
        await store.add_message("user" if i % 2 == 0 else "assistant", f"ход {i}", NOW + timedelta(minutes=i),
                                mode="rp", scene_id=scene.id)
    await roleplay.maybe_summarize(scene.id)
    updated = await store.get_scene(scene.id)
    assert updated.summary == "Рейн и Лира подружились, караван ушёл на север."
    window = await store.scene_messages(scene.id, limit=20)
    assert updated.summarized_until == window[0].id - 1


async def test_general_summary_skips_roleplay(store):
    provider = ScriptedProvider("{}", "Болтали о работе.")
    router = make_router(store, provider)
    updater = MemoryUpdater(store, router, history_limit=20, now=lambda: NOW)
    for i in range(16):
        await say(store, f"обычное {i}")
    for i in range(16):
        await say(store, f"*ролевой ход {i}*", mode="rp", scene_id=1)
    await updater.run(force=True)
    summary_prompt = provider.prompts[-1][1]["content"]
    assert "обычное" in summary_prompt and "ролевой ход" not in summary_prompt
