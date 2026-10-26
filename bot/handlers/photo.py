"""Входящие фото: описание через vision-модель, дальше как обычное сообщение."""

import logging

from aiogram import Bot, F, Router
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from bot.handlers.chat import Replier
from bot.images import ImageService
from bot.sleep import SleepTracker

logger = logging.getLogger(__name__)

router = Router(name="photo")

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024


async def _handle_image(message: Message, file_id: str, bot: Bot, replier: Replier,
                        images: ImageService, sleep: SleepTracker) -> None:
    now = replier.dialogue.now()
    caption = message.caption or ""
    await sleep.on_user_message(caption, now)

    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        data = await bot.download(file_id)
        description = await images.describe_photo(data.read(), caption)
    if description is None:
        logger.warning("Фото не удалось распознать")

    await replier.dialogue.store.add_message("user", caption, now, has_image=True, image_description=description)
    replier.debouncer.poke()


@router.message(F.photo)
async def on_photo(message: Message, bot: Bot, replier: Replier, images: ImageService, sleep: SleepTracker) -> None:
    await _handle_image(message, message.photo[-1].file_id, bot, replier, images, sleep)


@router.message(F.document.mime_type.startswith("image/"))
async def on_image_document(message: Message, bot: Bot, replier: Replier, images: ImageService,
                            sleep: SleepTracker) -> None:
    if (message.document.file_size or 0) > MAX_DOCUMENT_BYTES:
        await message.answer("ой, оно слишком тяжёлое, не грузится(( скинь обычным фото?")
        return
    await _handle_image(message, message.document.file_id, bot, replier, images, sleep)
