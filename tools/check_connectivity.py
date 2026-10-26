"""Доступны ли с этой машины Telegram и агрегатор.

    python -m tools.check_connectivity
    docker compose run --rm nika python -m tools.check_connectivity"""

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
    # Короткий таймаут: при проверке не нужно ждать минутами, как при обычной работе
    provider = OpenAICompatibleProvider(settings.chat_base_url, settings.chat_api_key, timeout=20)
    start = time.perf_counter()
    try:
        alive = await asyncio.wait_for(provider.ping(), timeout=30)
    except TimeoutError:
        alive = False
    if not alive:
        print(f"❌ Агрегатор {settings.chat_base_url} не отвечает или ключ неверный")
        print("   Если включён VPN — возможно, агрегатор не пускает зарубежные адреса: "
              "его надо пускать мимо VPN, а через обход — только Telegram.")
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
