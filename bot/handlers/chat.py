"""Обработка обычных сообщений: сохраняем, ждём, пока я допишу, отвечаем как Ника."""

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile, Message
from aiogram.utils.chat_action import ChatActionSender

from bot.dialogue import Dialogue
from bot.humanize import Debouncer, initial_delay, split_parts, typing_delay
from bot.images import FALLBACK_TEXT as IMAGE_FALLBACK_TEXT
from bot.llm.base import LLMError
from bot.roleplay import (
    RESUME_NOTE,
    STOP_TEXT,
    RoleplayManager,
    detect_entry,
    is_ooc_text,
    is_stop,
)
from bot.sleep import SleepTracker

logger = logging.getLogger(__name__)

router = Router(name="chat")

FALLBACK_TEXT = "что-то я подвисла(( напиши чуть позже"
START_FIRST_TIME = ("Он только что открыл чат с тобой — это ваше самое первое сообщение. "
                    "Поздоровайся и коротко познакомься, как обычная девушка в переписке.")
START_AGAIN = "Он снова нажал /start. Просто поздоровайся, как будто он снова заглянул в чат."


class Replier:
    """Отправляет ответы Ники «по-человечески»: пауза, «печатает...», несколько сообщений."""

    def __init__(self, bot: Bot, chat_id: int, dialogue: Dialogue) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.dialogue = dialogue
        self.debouncer = Debouncer(self.reply)
        self._lock = asyncio.Lock()  # два ответа одновременно не генерируем

    async def reply(self, extra_note: str | None = None, silent_errors: bool = False,
                    ooc: bool = False) -> str | None:
        """Генерирует и отправляет ответ. Возвращает его текст (None — ничего не отправлено).
        silent_errors — не писать «подвисла» при сбое (когда Ника пишет первой, я ничего не ждал);
        ooc — ответ точно вне ролевой игры."""
        async with self._lock:
            return await self._reply(extra_note, silent_errors, ooc)

    async def _reply(self, extra_note: str | None, silent_errors: bool, ooc: bool) -> str | None:
        try:
            async with ChatActionSender.typing(bot=self.bot, chat_id=self.chat_id):
                await asyncio.sleep(initial_delay())
                reply = await self.dialogue.generate_reply(extra_note, ooc=ooc)
        except LLMError as e:
            logger.error("Нейросеть недоступна: %s", e)
            if not silent_errors:
                await self.bot.send_message(self.chat_id, FALLBACK_TEXT)
            return None
        if reply is None:
            return None

        for i, part in enumerate(split_parts(reply.text, reply.mode)):
            if i > 0:  # перед первой частью «печатала», пока шла генерация
                async with ChatActionSender.typing(bot=self.bot, chat_id=self.chat_id):
                    await asyncio.sleep(typing_delay(part))
            await self.bot.send_message(self.chat_id, part)

        if reply.image_scene and self.dialogue.images is not None:
            await self._send_image(reply.image_scene)
        return reply.text or f"[картинка: {reply.image_scene}]"

    async def _send_image(self, scene: str) -> None:
        store, now = self.dialogue.store, self.dialogue.now
        async with ChatActionSender.upload_photo(bot=self.bot, chat_id=self.chat_id):
            image = await self.dialogue.images.generate(scene)
        if image is None:
            await self.bot.send_message(self.chat_id, IMAGE_FALLBACK_TEXT)
            await store.add_message("assistant", IMAGE_FALLBACK_TEXT, now())
            return
        await self.bot.send_photo(self.chat_id, BufferedInputFile(image, filename="nika.jpg"))
        await store.add_message("assistant", "", now(), has_image=True, image_description=scene)


@router.message(CommandStart())
async def on_start(message: Message, replier: Replier) -> None:
    has_history = await replier.dialogue.store.last_message() is not None
    await replier.reply(START_AGAIN if has_history else START_FIRST_TIME)


@router.message(F.text)
async def on_text(message: Message, replier: Replier, sleep: SleepTracker, roleplay: RoleplayManager) -> None:
    now = replier.dialogue.now()
    store = replier.dialogue.store
    text = message.text
    # «спокойной ночи» / «доброе утро» и просто факт, что я пишу — для определения сна
    await sleep.on_user_message(text, now)

    rp = await roleplay.get()
    if rp.state in ("scene", "ooc", "setup") and is_stop(text):
        # «стоп» — сразу и без вопросов
        await store.add_message("user", text, now)
        await roleplay.stop()
        await message.answer(STOP_TEXT)
        await store.add_message("assistant", STOP_TEXT, now)
        return

    if rp.state == "":
        entry = detect_entry(text)
        scene = await roleplay.last_unfinished_scene() if entry == "continue" else None
        if scene is not None:  # «давай продолжим нашу историю»
            await store.add_message("user", text, now)
            await roleplay.resume(scene.id)
            await replier.reply(RESUME_NOTE.format(title=scene.title))
            return
        if entry is not None:  # «го поиграем в ролевку?» — начинаем договариваться
            await roleplay.start_setup()

    # Внутри сцены сообщения — ролевые, кроме ((вне роли))
    in_scene = rp.state == "scene" and not is_ooc_text(text)
    await store.add_message("user", text, now, mode="rp" if in_scene else "chat",
                            scene_id=rp.scene.id if in_scene else None)
    replier.debouncer.poke()  # ответ придёт, когда я перестану писать


@router.message()
async def not_text(message: Message) -> None:
    # Стикеры, голосовые, видео и т.п. пока не поддерживаются (фото — в handlers/photo.py)
    await message.answer("ой, у меня это не открывается(( напиши текстом?")
