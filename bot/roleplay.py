"""RP-режим.

Состояния: "" → setup (договариваемся) → scene (играем) ⇄ ooc (/ooc).
((текст)) — одна реплика вне роли, «стоп» останавливает сразу.
Действия персонажа в память не идут — только то, какие сюжеты мы любим."""

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from bot.llm.base import LLMError
from bot.llm.router import LLMRouter
from bot.memory.facts import format_messages, parse_json
from bot.memory.store import Scene, Store

logger = logging.getLogger(__name__)

KEY_STATE = "rp:state"
KEY_SCENE = "rp:scene_id"
KEY_SETUP_FROM = "rp:setup_from"   # с какого сообщения начали договариваться

SCENE_WINDOW = 20        # сколько последних сообщений сцены идёт в запрос
SUMMARY_BATCH = 20       # сколько «выпавших» сообщений сцены копить до обновления краткого содержания

STOP_TEXT = "ок, стоп) остановились. захочешь — продолжим потом (/scenes), или просто поболтаем"

_STOP = re.compile(r"^\s*(?:стоп|stop|/stop)\s*[!.]*\s*$", re.IGNORECASE)
_OOC = re.compile(r"^\s*\(\(.*\)\)\s*$", re.DOTALL)
_NEW_GAME = re.compile(
    r"\b(?:го|давай|давайте|может|хочу)\b[^.?!]{0,20}\b(?:поиграем|сыграем|поиграть|сыграть)\b[^.?!]{0,25}"
    r"(?:ролев|рп\b|rp\b|историю|сюжет)"
    r"|\b(?:го|давай|может)\s+(?:в\s+)?(?:ролевку|ролевуху|ролёвку|рп|rp)\b",
    re.IGNORECASE,
)
_CONTINUE = re.compile(r"\bпродолж\w*\s+(?:нашу\s+|ту\s+|нашу\s+ту\s+)?(?:историю|сцену|ролев\w*|игру|рп|rp)\b",
                       re.IGNORECASE)

SETUP_NOTE = (
    "Вы договариваетесь о текстовой ролевой игре — пока это обычная переписка, ВНЕ роли. "
    "Если он ещё не предложил идею — предложи 2–3 коротких варианта (жанр, кто кем играет, завязка). "
    "Если предложил — коротко уточни, чего не хватает (кто ты, кто он, где начинаем), и спроси «начинаем?». "
    "Саму сцену не начинай, пока не договорились."
)
START_SCENE_NOTE = (
    "Вы договорились, начинается ролевая сцена «{title}». Сеттинг: {setting}. Персонажи: {characters}. "
    "Завязка: {opening}. Начни сцену: опиши обстановку и своего персонажа, дай ему за что зацепиться. "
    "Не пиши за его персонажа."
)
RESUME_NOTE = ("Вы возвращаетесь к ролевой сцене «{title}». Одной-двумя фразами напомни, где остановились, "
               "и продолжи от лица своего персонажа.")
OOC_NOTE = ("Он вышел из роли, чтобы поболтать (ваша сцена «{title}» на паузе). Отвечай как ты сама, "
            "в своём обычном стиле.")
END_NOTE = "Вы только что закончили ролевую сцену «{title}». Скажи пару слов о ней вне роли, как ты сама."
SCENE_PAUSED_NOTE = ("У вас есть недоигранная ролевая сцена «{title}». Пиши вне роли; можешь, например, "
                     "сказать, что придумала, что будет дальше в вашей истории.")

SETUP_CHECK_SYSTEM = """Девушка Ника и парень договариваются о текстовой ролевой игре. Прочитай переписку и верни JSON:
{{"ready": true/false, "title": "короткое название сцены", "setting": "жанр, место, время",
  "characters": "его персонаж: …; персонаж Ники: …", "opening": "завязка одной фразой"}}
ready — true, если уже понятно, во что играем и кто кем, И он согласился начинать (или сам просит начать).{force}"""

SCENE_SUMMARY_SYSTEM = """Ты ведёшь краткое содержание текстовой ролевой сцены. Тебе дают прошлое краткое содержание
и следующие сообщения сцены. Напиши обновлённое краткое содержание: до 120 слов, от третьего лица, о персонажах
(не об игроках): что произошло, где сейчас персонажи, что между ними, какие нити сюжета открыты.
Ответь только текстом."""

SCENE_END_SYSTEM = """Ролевая сцена закончилась. Тебе дают её краткое содержание и последние сообщения. Верни JSON:
{"summary": "итоговое краткое содержание сцены, до 120 слов, о персонажах",
 "preference": "одна фраза о том, какие сюжеты любят играть эти двое, например: «Любит играть с Никой в фэнтези-приключения с наёмниками и тавернами»"}"""


@dataclass
class RPState:
    state: str              # "" | setup | scene | ooc
    scene: Scene | None


def is_stop(text: str) -> bool:
    return bool(_STOP.match(text))


def is_ooc_text(text: str) -> bool:
    return bool(_OOC.match(text))


def detect_entry(text: str) -> str | None:
    """«давай продолжим нашу историю» → "continue", «го поиграем в ролевку?» → "new"."""
    if _CONTINUE.search(text):
        return "continue"
    if _NEW_GAME.search(text):
        return "new"
    return None


class RoleplayManager:
    def __init__(self, store: Store, router: LLMRouter, now: Callable[[], datetime]) -> None:
        self.store = store
        self.router = router
        self.now = now
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    # ---------- Состояние ----------

    async def get(self) -> RPState:
        state = await self.store.get_setting(KEY_STATE, "")
        scene_id = await self.store.get_setting(KEY_SCENE, "")
        scene = await self.store.get_scene(int(scene_id)) if scene_id.isdigit() else None
        if state in ("scene", "ooc") and scene is None:
            state = ""
        return RPState(state, scene)

    async def _set(self, state: str, scene_id: int | None = None) -> None:
        await self.store.set_setting(KEY_STATE, state)
        if scene_id is not None:
            await self.store.set_setting(KEY_SCENE, str(scene_id))

    async def last_unfinished_scene(self) -> Scene | None:
        for scene in await self.store.list_scenes(limit=20):
            if scene.status != "ended":
                return scene
        return None

    # ---------- Переходы ----------

    async def start_setup(self) -> None:
        last = await self.store.last_message()
        await self.store.set_setting(KEY_SETUP_FROM, str(last.id if last else 0))
        await self._set("setup")

    async def check_setup(self, force: bool = False) -> tuple[Scene, str] | None:
        """Договорились ли о сцене. Если да — создаёт сцену; возвращает (сцена, завязка)."""
        setup_from = int(await self.store.get_setting(KEY_SETUP_FROM, "0"))
        messages = await self.store.messages_between(setup_from, limit=40)
        if not messages and not force:
            return None
        force_text = ("\nОН ПРОСИТ НАЧАТЬ ПРЯМО СЕЙЧАС: верни ready=true и сам придумай всё, чего не хватает."
                      if force else "")
        try:
            result = await self.router.chat(
                [{"role": "system", "content": SETUP_CHECK_SYSTEM.format(force=force_text)},
                 {"role": "user", "content": format_messages(messages) or "переписки пока нет"}],
                purpose="service", temperature=0.2, max_tokens=400,
            )
            data = parse_json(result.text)
        except (LLMError, ValueError) as e:
            logger.warning("Проверка «договорились ли о сцене» не удалась: %s", e)
            return None
        if not data.get("ready") and not force:
            return None
        scene = await self.create_scene(str(data.get("title") or "Наша история"), str(data.get("setting", "")),
                                        str(data.get("characters", "")))
        return scene, str(data.get("opening", ""))

    async def create_scene(self, title: str, setting: str, characters: str) -> Scene:
        await self._pause_current()
        scene_id = await self.store.create_scene(title, setting, characters, self.now())
        await self._set("scene", scene_id)
        logger.info("Новая RP-сцена «%s»", title)
        return await self.store.get_scene(scene_id)

    async def resume(self, scene_id: int) -> Scene | None:
        scene = await self.store.get_scene(scene_id)
        if scene is None:
            return None
        current = await self.get()
        if current.scene and current.scene.id != scene_id:
            await self._pause_current()
        await self.store.update_scene(scene_id, self.now(), status="active")
        await self._set("scene", scene_id)
        return await self.store.get_scene(scene_id)

    async def ooc(self) -> Scene | None:
        current = await self.get()
        if current.state != "scene":
            return None
        await self._set("ooc")
        return current.scene

    async def back_to_scene(self) -> Scene | None:
        current = await self.get()
        if current.state != "ooc":
            return None
        await self._set("scene")
        return current.scene

    async def stop(self) -> Scene | None:
        """«стоп»: сразу ставим сцену на паузу и выходим из игры."""
        current = await self.get()
        await self._pause_current()
        await self._set("")
        return current.scene

    async def cancel_setup(self) -> None:
        await self._set("")

    async def end(self) -> Scene | None:
        """/end: итог сцены, запоминаем, какие сюжеты мы любим."""
        current = await self.get()
        scene = current.scene
        await self._set("")
        if scene is None or current.state not in ("scene", "ooc"):
            return None
        now = self.now()
        tail = await self.store.scene_messages(scene.id, after_id=scene.summarized_until, limit=60)
        summary = scene.summary
        try:
            result = await self.router.chat(
                [{"role": "system", "content": SCENE_END_SYSTEM},
                 {"role": "user", "content": f"Краткое содержание: {scene.summary or 'пока нет'}\n\n"
                                             f"Последние сообщения:\n{format_messages(tail)}"}],
                purpose="service", temperature=0.3, max_tokens=600,
            )
            data = parse_json(result.text)
            summary = str(data.get("summary") or summary).strip()
            preference = str(data.get("preference") or "").strip()
            if preference:
                await self._remember_preference(preference, now)
        except (LLMError, ValueError) as e:
            logger.warning("Итог сцены не получился: %s", e)
        await self.store.update_scene(scene.id, now, status="ended", summary=summary)
        return await self.store.get_scene(scene.id)

    async def _pause_current(self) -> None:
        current = await self.get()
        if current.scene and current.scene.status == "active":
            await self.store.update_scene(current.scene.id, self.now(), status="paused")

    async def _remember_preference(self, text: str, now: datetime) -> None:
        """«Любим играть в такие-то сюжеты» — это факт о нас (в отличие от действий персонажей)."""
        known = {f.text.casefold() for f in await self.store.all_facts()}
        if text.casefold() not in known:
            await self.store.add_fact("предпочтения", text, 2, now)

    # ---------- Длинные сцены ----------

    def schedule_summary(self, scene_id: int) -> None:
        if self._lock.locked():
            return
        task = asyncio.create_task(self.maybe_summarize(scene_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def maybe_summarize(self, scene_id: int) -> None:
        """Сообщения сцены, выпавшие из окна, сворачиваются в её краткое содержание."""
        async with self._lock:
            scene = await self.store.get_scene(scene_id)
            if scene is None:
                return
            window = await self.store.scene_messages(scene_id, limit=SCENE_WINDOW)
            if not window:
                return
            dropped = [m for m in await self.store.scene_messages(scene_id, after_id=scene.summarized_until,
                                                                  limit=200, last=False)
                       if m.id < window[0].id]
            if len(dropped) < SUMMARY_BATCH:
                return
            try:
                result = await self.router.chat(
                    [{"role": "system", "content": SCENE_SUMMARY_SYSTEM},
                     {"role": "user", "content": f"Прошлое краткое содержание: {scene.summary or 'пока нет'}\n\n"
                                                 f"Следующие сообщения:\n{format_messages(dropped)}"}],
                    purpose="service", temperature=0.3, max_tokens=500,
                )
            except LLMError as e:
                logger.warning("Краткое содержание сцены не обновлено: %s", e)
                return
            if result.text.strip():
                await self.store.update_scene(scene_id, self.now(), summary=result.text.strip(),
                                              summarized_until=dropped[-1].id)
