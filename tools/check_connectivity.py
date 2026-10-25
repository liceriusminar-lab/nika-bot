"""Проверка, что с этой машины доступны Telegram и агрегатор нейросетей.

На ПК:
    .\\.venv\\Scripts\\python.exe -m tools.check_connectivity
На сервере (бот при этом может работать):
    docker compose run --rm nika python -m tools.check_connectivity
"""

import asyncio
import sys
import time

from aiogram import Bot

from bot.config import get_settings
from bot.llm.openai_compatible import OpenAICompatibleProvider
from bot.main import make_telegram_session


async def check_telegram(settings) -> bool:
    bot = Bot(token=settings.telegram_bot_token, session=make_telegram_session(settings))
    start = time.perf_counter()
    try:
        me = await asyncio.wait_for(bot.get_me(), timeout=20)
        print(f"✅ Telegram: бот @{me.username} отвечает ({time.perf_counter() - start:.1f} с)")
        return True
    except Exception as e:  # noqa: BLE001 — показываем любую ошибку как есть
        print(f"❌ Telegram недоступен: {type(e).__name__}: {e}")
        print("   Похоже, хостинг блокирует API Telegram. Варианты: VPS в другой стране "
              "или TELEGRAM_PROXY / TELEGRAM_API_URL в .env (см. README).")
        return False
    finally:
        await bot.session.close()


async def check_aggregator(settings) -> bool:
    provider = OpenAICompatibleProvider(settings.chat_base_url, settings.chat_api_key)
    start = time.perf_counter()
    if not await provider.ping():
        print(f"❌ Агрегатор {settings.chat_base_url} недоступен или ключ неверный")
        return False
    balance = await provider.get_balance()
    extra = f", баланс {balance:.2f} ₽" if balance is not None else ""
    print(f"✅ Агрегатор: отвечает ({time.perf_counter() - start:.1f} с){extra}")
    return True


async def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    settings = get_settings()
    ok = await check_telegram(settings) & await check_aggregator(settings)
    print("\nВсё в порядке, можно запускать бота" if ok else "\nЕсть проблемы — см. выше")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
