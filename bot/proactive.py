"""«Пишет первой».

Раз в ~20 минут: жёсткие правила (сон, лимиты, не мешать разговору, не спамить) →
повод, у каждого своя вероятность, чтобы время было случайным → дешёвая модель
решает YES/SKIP → текст пишет основная модель."""

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from bot.llm.base import LLMError
from bot.llm.router import LLMRouter
from bot.memory.facts import format_messages
from bot.memory.store import Store
from bot.sleep import SleepTracker

logger = logging.getLogger(__name__)

KEY_ENABLED = "proactive:enabled"
KEY_PER_DAY = "proactive:per_day"
STALE_FOLLOWUP = timedelta(days=3)   # о событиях старше этого уже не спрашиваем


@dataclass
class Timings:
    check_seconds: int          # как часто проверять
    min_gap: timedelta          # минимум между её сообщениями первой
    active_guard: timedelta     # если переписка была недавно — не встревать
    silence_min: timedelta      # «давно молчит» — случайный порог от…
    silence_max: timedelta      # …и до
    missing_after: timedelta    # молчит дольше — одно «ты как? пропал куда-то»
    test: bool = False          # тестовый режим: без вероятностей и без утра/вечера


NORMAL = Timings(1200, timedelta(hours=2.5), timedelta(hours=1),
                 timedelta(hours=4), timedelta(hours=7), timedelta(hours=24))
TEST = Timings(60, timedelta(minutes=3), timedelta(minutes=1),
               timedelta(minutes=2), timedelta(minutes=4), timedelta(minutes=10), test=True)

_START = "Сейчас ты пишешь ему первой — его сообщения нет, это ты начинаешь разговор. "
NOTES = {
    "morning": _START + "Утро, он, скорее всего, недавно проснулся. Напиши короткое утреннее сообщение, "
                        "как девушка в мессенджере. Не повторяй свои прошлые утренние сообщения.",
    "evening": _START + "Вечер. Спроси, как прошёл его день, или поделись своим — коротко и живо.",
    "followup": _START + "Раньше он упоминал событие, о котором ты хотела спросить: «{text}». "
                         "Спроси, как всё прошло, естественно и коротко.",
    "silence": _START + "Он давно не писал. Напиши что-нибудь лёгкое — без упрёков за молчание.",
    "share": _START + "Поделись чем-то из своей жизни: рисунок, Соня, учёба, игра, аниме, что-то смешное. "
                      "Коротко, как другу. Можешь продолжить свои недавние события.",
    "missing": _START + "Он не отвечает больше суток. Напиши одно мягкое короткое сообщение в духе "
                        "«ты как? пропал куда-то» — без упрёков и драмы.",
}

GATE_SYSTEM = """Ты помогаешь девушке Нике решить, уместно ли ПРЯМО СЕЙЧАС написать парню первой.
Повод уже выбран, правила по времени соблюдены. Посмотри на конец переписки и на время сообщений:
- SKIP, если он недавно (с учётом времени — занятость ещё не закончилась) сказал, что занят, за рулём, на совещании, просил не писать; или разговор закончился ссорой и новое сообщение будет навязчивым;
- SKIP, если повод уже обсуждали в последних сообщениях, или событие, о котором хочется спросить, ещё явно не наступило;
- «пойду работать», «ушёл по делам» несколько часов назад — НЕ причина молчать, люди пишут друг другу и после такого;
- во всех остальных случаях YES.
Ответь одним словом: YES или SKIP."""


@dataclass
class Decision:
    reason: str
    note: str
    followup_id: int | None = None


class Replier(Protocol):
    async def reply(self, extra_note: str | None = None, silent_errors: bool = False,
                    ooc: bool = False) -> str | None: ...


class Proactive:
    def __init__(self, store: Store, sleep: SleepTracker, router: LLMRouter, replier: Replier | None,
                 default_per_day: int, timings: Timings = NORMAL, rng: random.Random | None = None) -> None:
        self.store = store
        self.sleep = sleep
        self.router = router
        self.replier = replier
        self.default_per_day = default_per_day
        self.t = timings
        self.rng = rng or random.Random()

    # ---------- Настройки ----------

    async def enabled(self) -> bool:
        return await self.store.get_setting(KEY_ENABLED, "1") == "1"

    async def per_day(self) -> int:
        return int(await self.store.get_setting(KEY_PER_DAY, str(self.default_per_day)))

    # ---------- Решение ----------

    def _chance(self, p: float) -> bool:
        return self.t.test or self.rng.random() < p

    def _silence_threshold(self, now: datetime) -> timedelta:
        """Порог «давно молчит» — случайный, но один и тот же в течение дня."""
        day_rng = random.Random(now.date().toordinal())
        lo, hi = self.t.silence_min.total_seconds(), self.t.silence_max.total_seconds()
        return timedelta(seconds=day_rng.uniform(lo, hi))

    async def decide(self, now: datetime) -> Decision | None:
        if not await self.enabled() or await self.sleep.is_asleep(now):
            return None

        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today = await self.store.proactive_since(day_start)
        if len(today) >= await self.per_day():
            return None

        last_pro = await self.store.last_proactive()
        if last_pro and now - last_pro.at < self.t.min_gap:
            return None

        last_any = await self.store.last_message()
        if last_any and now - last_any.created_at < self.t.active_guard:
            return None  # мы сейчас переписываемся

        last_user = await self.store.last_message_by("user")
        silence = now - last_user.created_at if last_user else None

        # Её прошлое сообщение первой осталось без ответа — больше не пишем.
        # Исключение: я молчу больше суток — одно мягкое «ты как?»
        if last_pro and (last_user is None or last_user.created_at < last_pro.at):
            if silence is not None and silence >= self.t.missing_after:
                since_user = await self.store.proactive_since(last_user.created_at)
                if not any(p.reason == "missing" for p in since_user):
                    return Decision("missing", NOTES["missing"])
            return None

        # Повод 1: пришёл срок спросить про событие
        for f in await self.store.due_followups(now):
            if now - f.due_at > STALE_FOLLOWUP:
                await self.store.mark_followup_done(f.id)  # слишком давно — уже неуместно
                continue
            if self._chance(0.5):
                return Decision("followup", NOTES["followup"].format(text=f.text), f.id)
            break

        reasons_today = {p.reason for p in today}
        hour = now.hour + now.minute / 60
        if not self.t.test:
            # Повод 2: утро — вскоре после того, как я обычно просыпаюсь, если я ещё не писал
            wake = await self.sleep.wake_hour(now)
            woke_at = day_start + timedelta(hours=wake)
            if (wake <= hour < wake + 2.5 and "morning" not in reasons_today
                    and (last_user is None or last_user.created_at < woke_at) and self._chance(0.3)):
                return Decision("morning", NOTES["morning"])
            # Повод 3: вечер — как прошёл день
            if 19 <= hour < 22.5 and "evening" not in reasons_today and self._chance(0.25):
                return Decision("evening", NOTES["evening"])

        # Повод 4: я давно молчу
        if silence is not None and silence >= self._silence_threshold(now) and self._chance(0.35):
            return Decision("silence", NOTES["silence"])

        # Повод 5: просто поделиться своей жизнью (иногда, днём)
        if not self.t.test and 11 <= hour < 22 and self._chance(0.08):
            return Decision("share", NOTES["share"])
        return None

    async def approve(self, decision: Decision, now: datetime) -> bool:
        """Дешёвая модель: уместно ли писать сейчас. При ошибке — не пишем."""
        recent = await self.store.recent_messages(10)
        user = (f"Сейчас {now:%d.%m %H:%M}. Повод написать: {decision.reason}.\n\n"
                f"Конец переписки:\n{format_messages(recent) if recent else 'переписки ещё не было'}")
        try:
            result = await self.router.chat(
                [{"role": "system", "content": GATE_SYSTEM}, {"role": "user", "content": user}],
                purpose="service", temperature=0, max_tokens=5,
            )
        except LLMError as e:
            logger.warning("Проверка «писать ли первой» не удалась: %s", e)
            return False
        return "YES" in result.text.upper()

    # ---------- Запуск из планировщика ----------

    async def check(self, now: datetime) -> str | None:
        """Одна проверка. Возвращает отправленный текст или None."""
        try:
            decision = await self.decide(now)
            if decision is None:
                return None
            if not await self.approve(decision, now):
                logger.info("Пишет первой (%s): модель решила, что сейчас не к месту", decision.reason)
                return None
            # Во время ролевой игры — только вне роли
            text = await self.replier.reply(decision.note, silent_errors=True, ooc=True)
            if not text:
                return None
            await self.store.add_proactive(decision.reason, text, now)
            if decision.followup_id is not None:
                await self.store.mark_followup_done(decision.followup_id)
            logger.info("Написала первой (%s)", decision.reason)
            return text
        except Exception:  # noqa: BLE001 — сбой проверки не должен ронять планировщик
            logger.exception("Ошибка в «пишет первой»")
            return None
