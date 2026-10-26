"""Сборка запроса к модели.

Порядок подобран под кэш провайдера: персона → память → история, а всё, что
меняется каждый раз (время, факты по теме, подсказки), — в самом конце."""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from bot.config import Settings
from bot.humanize import clean_text
from bot.images import ImageService, extract_marker
from bot.llm.router import LLMRouter
from bot.memory.facts import CATEGORY_TITLES, facts_for_context
from bot.memory.her_life import format_events, her_life_for_context
from bot.memory.store import Fact, Scene, Store, StoredMessage
from bot.memory.updater import MemoryUpdater
from bot.persona import build_system_prompt, build_time_note
from bot.relationship import STAGE_ORDER, RelationshipTracker
from bot.roleplay import (
    OOC_NOTE,
    SCENE_PAUSED_NOTE,
    SCENE_WINDOW,
    SETUP_NOTE,
    START_SCENE_NOTE,
    RoleplayManager,
    is_ooc_text,
)

logger = logging.getLogger(__name__)

HISTORY_LIMIT = 20

# Стадия, если трекер отношений не подключён (в тестах)
DEFAULT_STAGE = "acquaintances"

# Признаки кризиса: при них Ника обязана выйти из флирта и предложить помощь
_CRISIS = re.compile(
    r"не (?:хочу|хочется) (?:больше )?жить|жить не (?:хочу|хочется)|покончить с собой|покончу с собой"
    r"|(?:убить|убью) себя|суицид|самоубий|выпил(?:иться|юсь)|вскры(?:ть|ю) вены|(?:порезать|порежу|режу) себя"
    r"|самоповрежд|(?:всем|всем вокруг) (?:было бы|будет) (?:проще|лучше) без меня|лучше бы меня не было"
    r"|хочу исчезнуть|исчезнуть навсегда|нет смысла жить|уйти из жизни|наложить на себя руки",
    re.IGNORECASE,
)

CRISIS_NOTE = (
    "ВАЖНО: в последних сообщениях он, похоже, говорит о мыслях навредить себе или о сильном отчаянии. "
    "Сейчас никакого флирта и никакой игры. Ответь тепло и серьёзно, своими словами: покажи, что он тебе важен, "
    "спроси, что происходит, и обязательно мягко предложи поговорить с кем-то из близких или со специалистом "
    "(психолог, телефон доверия). Без лекций, не обрывай разговор, оставайся рядом."
)


NOTE_PREFIX = ("[подсказка для Ники — это пишет не он; не упоминай и не пересказывай её, "
               "просто ответь ему как Ника:")


def looks_like_crisis(text: str) -> bool:
    return bool(_CRISIS.search(text))


def _format_facts(facts: list[Fact]) -> str:
    return "\n".join(f"- {f.text} ({CATEGORY_TITLES.get(f.category, f.category)})" for f in facts)


@dataclass
class Reply:
    text: str                       # текст ответа (может быть пустым, если только картинка)
    image_scene: str | None = None  # что нарисовать, если Ника решила прислать картинку
    mode: str = "chat"              # chat | rp — от этого зависит, как резать ответ на сообщения


def _scene_block(scene: Scene) -> str:
    parts = [f"## Ролевая сцена «{scene.title}»"]
    if scene.setting:
        parts.append(f"Сеттинг: {scene.setting}")
    if scene.characters:
        parts.append(f"Персонажи: {scene.characters}")
    if scene.summary:
        parts.append(f"Что уже произошло в сцене: {scene.summary}")
    return "\n".join(parts)


class Dialogue:
    def __init__(self, settings: Settings, persona: dict[str, Any], store: Store, router: LLMRouter,
                 memory: MemoryUpdater | None = None, images: ImageService | None = None,
                 relationship: RelationshipTracker | None = None,
                 roleplay: RoleplayManager | None = None) -> None:
        self.settings = settings
        self.persona = persona
        self.store = store
        self.router = router
        self.memory = memory
        self.images = images
        self.relationship = relationship
        self.roleplay = roleplay

    def now(self) -> datetime:
        return datetime.now(self.settings.tz)

    async def _stage(self) -> tuple[str, bool]:
        """(стадия отношений, разрешена ли на ней ERP)."""
        if self.relationship is None:
            stage = DEFAULT_STAGE
            return stage, STAGE_ORDER.index(stage) >= STAGE_ORDER.index(self.settings.erp_min_stage)
        stage = await self.relationship.stage()
        return stage, await self.relationship.erp_allowed(stage)

    async def build_messages(self, history: list[StoredMessage], extra_note: str | None = None,
                             mode: str = "chat", scene: Scene | None = None) -> list[dict]:
        """mode="rp" — запрос для ответа внутри ролевой сцены scene."""
        now = self.now()
        stage, erp_allowed = await self._stage()
        system = build_system_prompt(self.persona, stage=stage, erp_allowed=erp_allowed, mode=mode)
        messages: list[dict[str, str]] = [{"role": "system", "content": system}]

        # Мои сообщения после последнего ответа Ники: по ним ищем факты и проверяем на кризис
        new_user_text = []
        for m in reversed(history):
            if m.role != "user":
                break
            new_user_text.insert(0, m.as_text())
        query = "\n".join(new_user_text) or (history[-1].content if history else "")

        important, relevant = await facts_for_context(self.store, query, now)
        recent_events, relevant_events = await her_life_for_context(self.store, query)
        memory_block = await self._memory_block(important, recent_events)
        if memory_block:
            messages.append({"role": "system", "content": memory_block})
        if mode == "rp" and scene is not None:
            messages.append({"role": "system", "content": _scene_block(scene)})

        # Подряд идущие сообщения одного автора склеиваем: так модели понятнее
        for m in history:
            role = "user" if m.role == "user" else "assistant"
            if messages[-1]["role"] == role:
                messages[-1]["content"] += "\n" + m.as_text()
            else:
                messages.append({"role": role, "content": m.as_text()})

        notes = [build_time_note(now)]
        if relevant or relevant_events:
            hint = ["Может пригодиться в ответе (используй, только если к месту):"]
            if relevant:
                hint.append("о нём:\n" + _format_facts(relevant))
            if relevant_events:
                hint.append("что ты сама рассказывала раньше:\n" + format_events(relevant_events))
            notes.append("\n".join(hint))
        if looks_like_crisis("\n".join(new_user_text)):
            logger.warning("Похоже на кризисное сообщение — Ника получает особую инструкцию")
            notes.append(CRISIS_NOTE)
        if self.images is not None and mode == "chat":  # в ролевой сцене картинок не шлём
            notes.append(await self.images.availability_note())
        if extra_note:
            notes.append(extra_note)

        # Подсказки идут служебной пометкой в моём последнем сообщении, а не отдельным системным:
        # на системное сообщение в конце DeepSeek иногда отвечает пустотой или продолжает его текстом
        note = f"{NOTE_PREFIX}\n" + "\n\n".join(notes) + "]"
        if messages[-1]["role"] == "user":
            messages[-1]["content"] += "\n\n" + note
        else:
            # Моего сообщения в конце нет (/start, а позже — «пишет первой»)
            messages.append({"role": "user", "content": note})
        return messages

    async def _memory_block(self, important: list[Fact], recent_events) -> str:
        parts = []
        if important:
            parts.append("## Что ты знаешь о нём\n" + _format_facts(important))
        summary = await self.store.latest_summary()
        if summary and summary.text:
            parts.append("## Что было раньше в вашей переписке\n" + summary.text)
        if recent_events:
            parts.append("## Что ты недавно рассказывала о себе (не противоречь этому)\n"
                         + format_events(recent_events))
        if not parts:
            return ""
        parts.append("Пользуйся памятью естественно, как человек: не перечисляй факты "
                     "и не говори «я помню, что…» без повода.")
        return "\n\n".join(parts)

    async def _roleplay_context(self, last: StoredMessage | None, ooc: bool) -> tuple[str, Scene | None, list[str]]:
        """Решает, отвечать ли внутри ролевой сцены. (mode, сцена, подсказки для Ники)."""
        if self.roleplay is None:
            return "chat", None, []
        rp = await self.roleplay.get()
        notes: list[str] = []
        if rp.state == "setup" and not ooc:
            created = await self.roleplay.check_setup()
            if created is None:
                return "chat", None, [SETUP_NOTE]
            scene, opening = created
            return "rp", scene, [START_SCENE_NOTE.format(title=scene.title, setting=scene.setting,
                                                         characters=scene.characters, opening=opening)]
        if rp.scene is None or rp.state not in ("scene", "ooc"):
            return "chat", None, notes
        if ooc:  # пишет первой — всегда вне роли
            return "chat", rp.scene, [SCENE_PAUSED_NOTE.format(title=rp.scene.title)]
        if rp.state == "ooc" or (last is not None and last.role == "user"
                                 and (is_ooc_text(last.content) or last.has_image)):
            # /ooc, моё сообщение в ((двойных скобках)) или фото — Ника отвечает как она сама
            return "chat", rp.scene, [OOC_NOTE.format(title=rp.scene.title)]
        return "rp", rp.scene, notes

    async def generate_reply(self, extra_note: str | None = None, ooc: bool = False) -> Reply | None:
        """Генерирует ответ Ники и сохраняет его текст. None — если отвечать не на что.
        Если Ника хочет прислать картинку — в Reply.image_scene её описание (рисует Replier).
        ooc=True — ответ точно вне ролевой игры (когда Ника пишет первой)."""
        recent = await self.store.recent_messages(HISTORY_LIMIT)
        last = recent[-1] if recent else None
        if extra_note is None and (last is None or last.role != "user"):
            return None  # последнее слово уже за Никой

        mode, scene, notes = await self._roleplay_context(last, ooc)
        if extra_note:
            notes.append(extra_note)
        # В сцене — её последние сообщения, иначе — обычная история (вместе с ролевыми)
        history = await self.store.scene_messages(scene.id, limit=SCENE_WINDOW) if mode == "rp" else recent

        messages = await self.build_messages(history, "\n\n".join(notes) or None, mode=mode, scene=scene)
        result = await self.router.chat(messages, purpose="chat", max_tokens=800 if mode == "rp" else 500)
        text, image = extract_marker(clean_text(result.text))
        if mode == "rp":
            image = None
        if not text and not image:
            return None
        if text:
            await self.store.add_message("assistant", text, self.now(), mode=mode,
                                         scene_id=scene.id if mode == "rp" else None)
        logger.info("Ответ от %s (%s): %d→%d ток. (кэш %d)%s", result.model, mode, result.input_tokens,
                    result.output_tokens, result.cached_tokens, f", картинка: {image}" if image else "")
        if self.memory is not None:
            self.memory.schedule()
        if self.relationship is not None:
            self.relationship.schedule()
        if mode == "rp" and self.roleplay is not None:
            self.roleplay.schedule_summary(scene.id)
        return Reply(text, image, mode)
