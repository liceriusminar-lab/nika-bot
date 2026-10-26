"""Чистка ответа, разбивка на сообщения, паузы и ожидание, пока я допишу."""

import asyncio
import logging
import random
import re
from typing import Awaitable, Callable

from bot.persona import MESSAGE_SEPARATOR

logger = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 5.0      # сколько ждать, не напишу ли я ещё что-то
MAX_PARTS = 3               # максимум отдельных сообщений в одном ответе
TELEGRAM_LIMIT = 4000       # у Telegram предел 4096 символов на сообщение

# Фразы ассистента, которые живая Ника не написала бы
_ASSISTANT_PHRASES = re.compile(
    r"(?:чем (?:ещё )?(?:я )?могу (?:вам |тебе )?помочь\??"
    r"|если (?:у (?:тебя|вас) )?(?:есть|будут|появятся) (?:ещё )?(?:какие-(?:то|либо) )?вопросы[^.!?\n]*[.!?]?"
    r"|обращайтесь[.!]?)",
    re.IGNORECASE,
)


def clean_text(text: str) -> str:
    """Убирает markdown, списки, подпись «Ника:» и ассистентские фразы."""
    text = text.strip()
    text = re.sub(r"^\s*ника\s*:\s*", "", text, flags=re.IGNORECASE)   # «Ника: привет» -> «привет»
    text = re.sub(r"```[a-z]*\n?|`", "", text)                         # блоки кода
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)     # **жирный**
    text = re.sub(r"__(.+?)__", r"\1", text, flags=re.DOTALL)         # __подчёркнутый__
    text = re.sub(r"^\s*#{1,6}\s+", "", text, flags=re.MULTILINE)     # # заголовки
    # Маркеры списков в начале строки. «*действие*» в RP не трогаем: после * там нет пробела
    text = re.sub(r"^\s*(?:[-•]|\*(?=\s)|\d+[.)])\s+", "", text, flags=re.MULTILINE)
    text = _ASSISTANT_PHRASES.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_parts(text: str, mode: str = "chat") -> list[str]:
    """Режет ответ на отдельные сообщения по ||| и пустым строкам (абзацам).
    В режиме RP — одно сообщение."""
    if mode == "rp":
        parts = [text.replace(MESSAGE_SEPARATOR, "\n\n")]
    else:
        # В мессенджере абзацы — это обычно отдельные сообщения
        parts = [p.strip() for p in re.split(r"\|\|\||\n\s*\n", text) if p.strip()]
        if len(parts) > MAX_PARTS:
            parts = parts[: MAX_PARTS - 1] + ["\n".join(parts[MAX_PARTS - 1:])]

    # Слишком длинные куски режем под лимит Telegram
    result = []
    for part in parts:
        while len(part) > TELEGRAM_LIMIT:
            cut = part.rfind("\n", 0, TELEGRAM_LIMIT)
            cut = cut if cut > 0 else TELEGRAM_LIMIT
            result.append(part[:cut].strip())
            part = part[cut:].strip()
        if part:
            result.append(part)
    return result


def typing_delay(text: str) -> float:
    """Сколько «печатать» сообщение: зависит от длины, от 1 до 6 секунд."""
    return min(6.0, max(1.0, 0.6 + len(text) * 0.035))


def initial_delay() -> float:
    """Небольшая случайная пауза перед ответом — человек не отвечает мгновенно."""
    return random.uniform(1.0, 4.0)


class Debouncer:
    """Ждёт, пока я перестану писать, и только потом запускает ответ.

    Каждое новое сообщение перезапускает таймер. Если ответ уже генерируется,
    его не прерываем: после него таймер сработает снова и Ника ответит на новое.
    """

    def __init__(self, action: Callable[[], Awaitable[None]], delay: float = DEBOUNCE_SECONDS) -> None:
        self.action = action
        self.delay = delay
        self._timer: asyncio.Task | None = None
        self._waiting = False  # таймер ещё спит — можно перезапустить
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()  # иначе задачу может собрать GC

    def poke(self) -> None:
        """Вызывать на каждое моё сообщение."""
        if self._timer is not None and self._waiting:
            self._timer.cancel()
        self._waiting = True
        self._timer = asyncio.create_task(self._wait_then_act())
        self._tasks.add(self._timer)
        self._timer.add_done_callback(self._tasks.discard)

    async def _wait_then_act(self) -> None:
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            return
        self._waiting = False
        async with self._lock:
            try:
                await self.action()
            except Exception:  # noqa: BLE001 — ошибка ответа не должна ронять бота
                logger.exception("Ошибка при ответе")
