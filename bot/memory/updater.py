"""Фоновое обновление памяти: запускается после ответа Ники и не задерживает его.

Каждые ~10 новых сообщений:
- извлекает факты, её события и followups (facts.extract_memory);
- сворачивает сообщения, которые уже не помещаются в окно истории, в сводку (summary.summarize).
"""

import asyncio
import logging
from datetime import datetime
from typing import Callable

from bot.llm.base import LLMError
from bot.llm.router import LLMRouter
from bot.memory.facts import extract_memory
from bot.memory.store import Store
from bot.memory.summary import summarize

logger = logging.getLogger(__name__)

EXTRACT_EVERY = 10         # через сколько новых сообщений извлекать факты
CONTEXT_MESSAGES = 6       # сколько предыдущих сообщений показать для понимания
SUMMARY_BATCH = 10         # сколько «выпавших» из окна сообщений копить до новой сводки

KEY_EXTRACTED = "memory:extracted_until"   # id последнего обработанного сообщения


class MemoryUpdater:
    def __init__(self, store: Store, router: LLMRouter, history_limit: int,
                 now: Callable[[], datetime]) -> None:
        self.store = store
        self.router = router
        self.history_limit = history_limit
        self.now = now
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    def schedule(self) -> None:
        """Запустить проверку в фоне (вызывается после каждого ответа)."""
        if self._lock.locked():
            return  # уже работает — следующий ответ запустит снова
        task = asyncio.create_task(self.run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def run(self, force: bool = False) -> None:
        async with self._lock:
            try:
                await self._extract(force)
                await self._summarize(force)
            except (LLMError, ValueError) as e:
                # ValueError — модель вернула кривой JSON. Не страшно: попробуем в следующий раз
                logger.warning("Обновление памяти не удалось: %s", e)
            except Exception:  # noqa: BLE001 — память не должна ронять бота
                logger.exception("Ошибка при обновлении памяти")

    async def _extract(self, force: bool) -> None:
        done_until = int(await self.store.get_setting(KEY_EXTRACTED, "0"))
        new = await self.store.messages_between(done_until, limit=EXTRACT_EVERY * 5)
        # Ролевые сообщения — не факты обо мне: это действия персонажей
        new_chat = [m for m in new if m.mode == "chat"]
        if not new or (len(new) < EXTRACT_EVERY and not force):
            return
        if new_chat:
            before = await self.store.messages_before(new_chat[0].id, CONTEXT_MESSAGES)
            stats = await extract_memory(self.router, self.store, new_chat, before, self.now())
            logger.info("Память: +%d фактов, %d уточнено, +%d о её жизни, +%d followups",
                        stats.new_facts, stats.updated_facts, stats.her_events, stats.followups)
        await self.store.set_setting(KEY_EXTRACTED, str(new[-1].id))

    async def _summarize(self, force: bool) -> None:
        window = await self.store.recent_messages(self.history_limit)
        if not window:
            return
        first_in_window = window[0].id
        previous = await self.store.latest_summary()
        done_until = previous.last_message_id if previous else 0
        # Сообщения, которые уже выпали из окна истории и ещё не свёрнуты
        dropped = await self.store.messages_between(done_until, up_to_id=first_in_window - 1)
        if not dropped or (len(dropped) < SUMMARY_BATCH and not force):
            return
        # У ролевых сцен свои краткие содержания (roleplay.py) — в общую сводку их не мешаем
        chat = [m for m in dropped if m.mode == "chat"]
        if not chat:
            await self.store.add_summary(previous.text if previous else "", dropped[0].created_at,
                                         dropped[-1].created_at, dropped[-1].id, self.now())
            return
        await summarize(self.router, self.store, chat, self.now(), last_message_id=dropped[-1].id)
        logger.info("Сводка обновлена (+%d сообщений)", len(chat))
