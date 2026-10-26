"""Запуск: python -m bot.main"""

import asyncio
import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import PRODUCTION, TelegramAPIServer
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.types import BotCommand
from aiogram.utils.token import TokenValidationError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from pydantic import ValidationError

from bot.backup import backup_if_missing, make_backup
from bot.config import Settings, get_settings
from bot.dialogue import HISTORY_LIMIT, Dialogue
from bot.handlers import chat, commands, photo
from bot.handlers.chat import Replier
from bot.handlers.owner_only import OwnerOnlyMiddleware
from bot.images import ImageService
from bot.llm.base import LLMError
from bot.llm.openai_compatible import OpenAICompatibleProvider
from bot.llm.router import LLMRouter
from bot.memory.store import Store
from bot.memory.updater import MemoryUpdater
from bot.persona import PersonaError, load_persona
from bot.proactive import NORMAL, TEST, Proactive
from bot.relationship import RelationshipTracker
from bot.roleplay import RoleplayManager
from bot.sleep import SleepTracker

logger = logging.getLogger("bot")

BOT_COMMANDS = [
    BotCommand(command="sleep", description="я ухожу спать"),
    BotCommand(command="wake", description="я проснулся"),
    BotCommand(command="status", description="кто мы друг другу"),
    BotCommand(command="settings", description="настройки: сообщения первой, картинки, стадия"),
    BotCommand(command="rp", description="ролевая игра: начать / вернуться в сцену"),
    BotCommand(command="ooc", description="выйти из роли"),
    BotCommand(command="end", description="закончить сцену"),
    BotCommand(command="scenes", description="наши сцены"),
    BotCommand(command="stop", description="сразу остановить сцену"),
    BotCommand(command="memory", description="что Ника обо мне помнит"),
    BotCommand(command="forget", description="забыть факт: /forget номер"),
    BotCommand(command="stats", description="расходы на нейросеть"),
    BotCommand(command="reset", description="стереть историю"),
]


def setup_logging(settings: Settings) -> None:
    log_dir = settings.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    file = RotatingFileHandler(log_dir / "bot.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    file.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[console, file])

    for noisy in ("httpx", "httpx2", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def make_providers(settings: Settings) -> tuple[OpenAICompatibleProvider, OpenAICompatibleProvider | None]:
    primary = OpenAICompatibleProvider(settings.chat_base_url, settings.chat_api_key,
                                       disable_reasoning=settings.chat_disable_reasoning)
    backup = None
    if settings.chat_backup_base_url and settings.chat_backup_api_key and settings.chat_backup_model:
        backup = OpenAICompatibleProvider(settings.chat_backup_base_url, settings.chat_backup_api_key,
                                          disable_reasoning=settings.chat_disable_reasoning)
    return primary, backup


def make_telegram_session(settings: Settings) -> AiohttpSession:
    api = TelegramAPIServer.from_base(settings.telegram_api_url) if settings.telegram_api_url else PRODUCTION
    if settings.telegram_proxy:
        logger.info("Telegram через прокси")
    if settings.telegram_api_url:
        logger.info("Telegram через релей %s", settings.telegram_api_url)
    return AiohttpSession(api=api, proxy=settings.telegram_proxy or None)


async def run(settings: Settings) -> None:
    persona = load_persona(settings.persona_file)
    logger.info("Персона загружена: %s, %s", persona["name"], persona["age"])

    if not settings.chat_model:
        raise LLMError("Не задана модель разговора CHAT_MODEL в .env")

    bot = Bot(token=settings.telegram_bot_token, session=make_telegram_session(settings))
    store = Store(settings.data_dir / "nika.db")
    await store.open()

    async def notify(text: str) -> None:
        await bot.send_message(settings.owner_telegram_id, text)

    primary, backup = make_providers(settings)
    llm_router = LLMRouter(settings, primary, store, notify, backup=backup)
    await llm_router.load_prices()

    def now() -> datetime:
        return datetime.now(settings.tz)

    memory = MemoryUpdater(store, llm_router, HISTORY_LIMIT, now=now)
    images = ImageService(settings, persona, store, llm_router, primary, now=now)
    relationship = RelationshipTracker(store, llm_router, settings, now=now)
    roleplay = RoleplayManager(store, llm_router, now=now)
    dialogue = Dialogue(settings, persona, store, llm_router, memory, images, relationship, roleplay)
    replier = Replier(bot, settings.owner_telegram_id, dialogue)

    sleep = SleepTracker(store)
    timings = TEST if settings.proactive_test_mode else NORMAL
    proactive = Proactive(store, sleep, llm_router, replier, settings.proactive_per_day, timings)

    # именно async def: лямбду, возвращающую корутину, APScheduler запустил бы в отдельном потоке
    async def proactive_tick() -> None:
        await proactive.check(datetime.now(settings.tz))

    scheduler = AsyncIOScheduler(timezone=settings.tz)
    scheduler.add_job(proactive_tick,
                      IntervalTrigger(seconds=timings.check_seconds, jitter=timings.check_seconds // 10),
                      max_instances=1, coalesce=True)
    if settings.proactive_test_mode:
        logger.warning("ТЕСТОВЫЙ РЕЖИМ «пишет первой»: интервалы в минутах вместо часов")

    backups_dir = settings.data_dir / "backups"

    async def backup_tick() -> None:
        try:
            await make_backup(store, backups_dir, now())
        except Exception:  # noqa: BLE001
            logger.exception("Бэкап не удался")

    scheduler.add_job(backup_tick, CronTrigger(hour=4, minute=30), max_instances=1, coalesce=True)
    await backup_if_missing(store, backups_dir, now())

    dp = Dispatcher(settings=settings, store=store, llm_router=llm_router, replier=replier, memory=memory,
                    sleep=sleep, proactive=proactive, images=images, relationship=relationship,
                    roleplay=roleplay)
    dp.update.outer_middleware(OwnerOnlyMiddleware(settings.owner_telegram_id))
    # порядок важен: в chat.router последний хендлер ловит всё подряд
    dp.include_router(commands.router)
    dp.include_router(photo.router)
    dp.include_router(chat.router)

    try:
        me = await bot.get_me()
        await bot.set_my_commands(BOT_COMMANDS)
        logger.info("Бот @%s запущен (модель %s%s), отвечает только пользователю id=%s",
                    me.username, settings.chat_model, ", есть резерв" if backup else "",
                    settings.owner_telegram_id)
        scheduler.start()
        await dp.start_polling(bot)
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await store.close()
        await bot.session.close()


def main() -> None:
    try:
        settings = get_settings()
    except ValidationError as e:
        missing = ", ".join(str(err["loc"][0]).upper() for err in e.errors())
        print(f"Ошибка в настройках (.env): проверь поля {missing}")
        print("Если .env ещё нет — скопируй .env.example и заполни.")
        sys.exit(1)

    setup_logging(settings)

    try:
        asyncio.run(run(settings))
    except PersonaError as e:
        logger.error("Проблема с persona.yaml: %s", e)
        sys.exit(1)
    except LLMError as e:
        logger.error("Проблема с настройками нейросети: %s", e)
        sys.exit(1)
    except (TokenValidationError, TelegramUnauthorizedError):
        logger.error("Telegram не принял токен. Проверь TELEGRAM_BOT_TOKEN в .env (его выдаёт @BotFather)")
        sys.exit(1)
    except TelegramNetworkError as e:
        logger.error("Нет связи с серверами Telegram: %s. Проверь интернет и попробуй ещё раз", e)
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Бот остановлен")


if __name__ == "__main__":
    main()
