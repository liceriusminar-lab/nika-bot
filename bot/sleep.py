"""Сплю ли я: /sleep и /wake, фразы вроде «спокойной ночи», окно по активности
за две недели, а пока данных мало — 01:00–09:00."""

import math
import re
from datetime import datetime, timedelta

from bot.memory.store import Store

DEFAULT_WINDOW = (1, 9)          # с 01:00 до 09:00
LEARN_DAYS = 14
MIN_MESSAGES_TO_LEARN = 40
MIN_DAYS_TO_LEARN = 5
MIN_SIGNALS_TO_LEARN = 3          # столько /sleep и /wake, чтобы считать окно по ним
MIN_SLEEP = timedelta(hours=6)    # после стольких часов сна и вне окна — считаем, что проснулся
MAX_SLEEP = timedelta(hours=12)   # «сплю» дольше этого не держится

KEY_STATE = "sleep:state"         # asleep | awake
KEY_STATE_AT = "sleep:state_at"

SLEEP_PHRASES = re.compile(
    r"спокойной ночи|доброй ночи|сладких снов|\bспоки\b|\bотбой\b"
    r"|\b(?:я|пойду|иду|ушёл|ушел|ложусь)\s+(?:уже\s+)?спать",
    re.IGNORECASE,
)
WAKE_PHRASES = re.compile(
    r"доброе утро|доброго утра|с добрым утром|\bутречк|\bпросну(?:лся|лась)\b|\bя\s+встал\b",
    re.IGNORECASE,
)


def in_window(hour: float, start: float, end: float) -> bool:
    """Попадает ли час в окно [start, end). Окно может переходить через полночь (23 → 7)."""
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


def _hour(dt: datetime) -> float:
    return dt.hour + dt.minute / 60


def _circular_mean_hour(hours: list[float]) -> float:
    """Среднее время суток с учётом перехода через полночь (среднее 23:00 и 01:00 — это 00:00)."""
    x = sum(math.cos(h / 24 * 2 * math.pi) for h in hours)
    y = sum(math.sin(h / 24 * 2 * math.pi) for h in hours)
    return (math.atan2(y, x) / (2 * math.pi) * 24) % 24


def learn_window(activity: list[tuple[str, datetime]]) -> tuple[int, int] | None:
    """Ищет моё обычное окно сна по истории активности. None — если данных пока мало."""
    sleeps = [_hour(at) for kind, at in activity if kind == "sleep"]
    wakes = [_hour(at) for kind, at in activity if kind == "wake"]
    if len(sleeps) >= MIN_SIGNALS_TO_LEARN and len(wakes) >= MIN_SIGNALS_TO_LEARN:
        return round(_circular_mean_hour(sleeps)) % 24, round(_circular_mean_hour(wakes)) % 24

    messages = [at for kind, at in activity if kind == "message"]
    if len(messages) < MIN_MESSAGES_TO_LEARN or len({m.date() for m in messages}) < MIN_DAYS_TO_LEARN:
        return None

    # Сколько сообщений было в каждый час суток; «тихий» час — почти без сообщений
    counts = [0] * 24
    for m in messages:
        counts[m.hour] += 1
    quiet = [c <= max(1, len(messages) * 0.01) for c in counts]
    if all(quiet) or not any(quiet):
        return None

    # Самая длинная серия тихих часов подряд (по кругу, через полночь)
    best_start, best_len = 0, 0
    for start in range(24):
        length = 0
        while length < 24 and quiet[(start + length) % 24]:
            length += 1
        if length > best_len:
            best_start, best_len = start, length
    if not 4 <= best_len <= 14:
        return None
    return best_start, (best_start + best_len) % 24


class SleepTracker:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def on_user_message(self, text: str, now: datetime) -> str | None:
        """Вызывается на каждое моё сообщение. Возвращает "sleep"/"wake", если состояние поменялось."""
        await self.store.add_activity("message", now)
        if SLEEP_PHRASES.search(text):
            await self.go_to_sleep(now)
            return "sleep"
        if WAKE_PHRASES.search(text):
            await self.wake_up(now)
            return "wake"
        state = await self.store.get_setting(KEY_STATE)
        state_at = await self.store.get_setting(KEY_STATE_AT)
        # Написал, хотя «спит». Если прошло меньше часа — это ещё прощание («ну всё, пока»), не пробуждение
        if state == "asleep" and state_at and now - datetime.fromisoformat(state_at) >= timedelta(hours=1):
            await self.wake_up(now)
            return "wake"
        return None

    async def go_to_sleep(self, now: datetime) -> None:
        await self.store.add_activity("sleep", now)
        await self._set_state("asleep", now)

    async def wake_up(self, now: datetime) -> None:
        await self.store.add_activity("wake", now)
        await self._set_state("awake", now)

    async def window(self, now: datetime) -> tuple[int, int, bool]:
        """(начало, конец, выучено ли по истории)."""
        learned = learn_window(await self.store.activity_since(now - timedelta(days=LEARN_DAYS)))
        if learned:
            return learned[0], learned[1], True
        return DEFAULT_WINDOW[0], DEFAULT_WINDOW[1], False

    async def wake_hour(self, now: datetime) -> int:
        """Во сколько я обычно просыпаюсь."""
        return (await self.window(now))[1]

    async def is_asleep(self, now: datetime) -> bool:
        start, end, _ = await self.window(now)
        in_usual_window = in_window(_hour(now), start, end)

        state = await self.store.get_setting(KEY_STATE)
        state_at = await self.store.get_setting(KEY_STATE_AT)
        if state and state_at:
            since = now - datetime.fromisoformat(state_at)
            if state == "asleep" and since < MAX_SLEEP:
                # Явно ушёл спать. Проснулся, только если поспал достаточно и уже не ночь
                return not (since >= MIN_SLEEP and not in_usual_window)
            if state == "awake" and since < timedelta(hours=3):
                return False  # недавно явно проснулся или писал посреди «ночи»
        return in_usual_window

    async def _set_state(self, state: str, now: datetime) -> None:
        await self.store.set_setting(KEY_STATE, state)
        await self.store.set_setting(KEY_STATE_AT, now.isoformat())
