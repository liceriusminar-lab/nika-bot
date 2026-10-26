"""Уровень близости 0–100 и стадии: знакомые → друзья (30) → близкие (60) → романтика (85,
только если он сам проявлял интерес).

Раз в ~30 сообщений или раз в день дешёвая модель ставит оценку от −3 до +3,
рост ограничен +3 в день. Ролевые сцены не учитываются."""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Callable

from bot.config import Settings
from bot.llm.base import LLMError
from bot.llm.router import LLMRouter
from bot.memory.facts import format_messages, parse_json
from bot.memory.store import Store
from bot.persona import STAGES

logger = logging.getLogger(__name__)

STAGE_ORDER = ["acquaintances", "friends", "close_friends", "romance"]
STAGE_THRESHOLDS = {"acquaintances": 0, "friends": 30, "close_friends": 60, "romance": 85}

START_LEVEL = 15.0          # «мы недавно познакомились и общаемся как друзья»
MAX_DELTA = 3               # оценка за раз: от −3 до +3
MAX_GAIN_PER_DAY = 3.0      # рост за сутки — не больше
EVALUATE_EVERY = 30         # сообщений между оценками
MIN_MESSAGES_FOR_DAILY = 6  # раз в день — если набралось хотя бы столько сообщений
MAX_MESSAGES_TO_READ = 60

KEY_LEVEL = "relationship:level"
KEY_INTEREST = "relationship:his_interest"      # проявлял ли он романтический интерес (запоминается)
KEY_FIXED = "relationship:fixed_stage"          # стадия, зафиксированная вручную
KEY_ERP = "relationship:erp_min_stage"          # с какой стадии ERP (переопределяет .env)
KEY_EVALUATED_UNTIL = "relationship:evaluated_until"
KEY_EVALUATED_AT = "relationship:evaluated_at"

EVALUATE_SYSTEM = """Ты оцениваешь, как развиваются отношения девушки Ники с парнем, по их недавней переписке.
Текущая стадия: {stage}. Верни JSON:
{{"delta": целое от -3 до 3, "reason": "коротко, почему", "romantic_interest": true/false}}

delta:
+3 — редко: очень тёплый, искренний, взаимный разговор, он открылся, поддержал её или она его;
+1…+2 — обычное тёплое общение, интерес друг к другу, общие шутки;
0 — сухо, коротко, по делу, ничего не поменялось;
−1…−3 — грубость, оскорбления, холодность, неуважение, давление.
Отношения растут медленно: не завышай. Настойчивые попытки сразу перейти к флирту или сексу — не повод для роста.
romantic_interest — true, только если ОН сам явно проявлял романтический интерес к Нике (флирт, признание, «нравишься»)."""

# Как Ника описывает ваши отношения в /status
STATUS_TEXT = {
    "acquaintances": "мы только знакомимся) но ты мне уже нравишься как собеседник",
    "friends": "мы друзья) мне с тобой легко и интересно",
    "close_friends": "ты для меня близкий человек, правда. могу рассказать тебе почти всё",
    "romance": "кажется, я в тебя влюбилась… и мне кажется, это взаимно)",
}


def stage_for(level: float, his_interest: bool) -> str:
    """Стадия по уровню. Романтика — только если он сам проявлял интерес."""
    stage = "acquaintances"
    for name in STAGE_ORDER:
        if level >= STAGE_THRESHOLDS[name]:
            stage = name
    if stage == "romance" and not his_interest:
        return "close_friends"
    return stage


def stage_label(stage: str) -> str:
    return STAGES[stage][0]


class RelationshipTracker:
    def __init__(self, store: Store, router: LLMRouter, settings: Settings, now: Callable[[], datetime]) -> None:
        self.store = store
        self.router = router
        self.settings = settings
        self.now = now
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    # ---------- Текущее состояние ----------

    async def level(self) -> float:
        return float(await self.store.get_setting(KEY_LEVEL, str(START_LEVEL)))

    async def his_interest(self) -> bool:
        return await self.store.get_setting(KEY_INTEREST, "0") == "1"

    async def fixed_stage(self) -> str | None:
        value = await self.store.get_setting(KEY_FIXED)
        return value if value in STAGE_ORDER else None

    async def stage(self) -> str:
        return await self.fixed_stage() or stage_for(await self.level(), await self.his_interest())

    async def erp_min_stage(self) -> str:
        value = await self.store.get_setting(KEY_ERP)
        return value if value in STAGE_ORDER else self.settings.erp_min_stage

    async def erp_allowed(self, stage: str | None = None) -> bool:
        stage = stage or await self.stage()
        return STAGE_ORDER.index(stage) >= STAGE_ORDER.index(await self.erp_min_stage())

    async def status_text(self) -> str:
        """Ответ на /status: по-человечески, без чисел."""
        stage = await self.stage()
        text = STATUS_TEXT[stage]
        if await self.fixed_stage():
            return text + "\n\n(стадия зафиксирована в /settings)"
        level = await self.level()
        index = STAGE_ORDER.index(stage)
        if index + 1 < len(STAGE_ORDER):
            nxt = STAGE_THRESHOLDS[STAGE_ORDER[index + 1]]
            if nxt - level <= 7:
                text += "\nи, кажется, мы становимся ещё ближе"
        week = await self.store.relationship_changes_since(self.now() - timedelta(days=7))
        if sum(d for d, *_ in week) <= -3:
            text += "\nхотя в последнее время между нами как-то прохладно("
        return text

    # ---------- Изменение уровня ----------

    async def apply(self, delta: float, reason: str, romantic_interest: bool, now: datetime) -> float:
        """Применяет оценку с учётом ограничений. Возвращает новый уровень."""
        delta = max(-MAX_DELTA, min(MAX_DELTA, delta))
        if delta > 0:
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            gained = sum(d for d, *_ in await self.store.relationship_changes_since(day_start) if d > 0)
            delta = min(delta, max(0.0, MAX_GAIN_PER_DAY - gained))
        old_stage = await self.stage()
        level = max(0.0, min(100.0, await self.level() + delta))
        await self.store.set_setting(KEY_LEVEL, f"{level:.2f}")
        if romantic_interest:
            await self.store.set_setting(KEY_INTEREST, "1")
        await self.store.add_relationship_change(delta, level, reason, now)
        new_stage = await self.stage()
        if new_stage != old_stage:
            logger.info("Стадия отношений: %s → %s", stage_label(old_stage), stage_label(new_stage))
        return level

    def schedule(self) -> None:
        """Проверить в фоне, не пора ли оценить разговоры (вызывается после ответа Ники)."""
        if self._lock.locked():
            return
        task = asyncio.create_task(self.maybe_evaluate())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def maybe_evaluate(self, force: bool = False) -> bool:
        """Оценивает разговоры, если пора. True — оценка была."""
        async with self._lock:
            now = self.now()
            done_until = int(await self.store.get_setting(KEY_EVALUATED_UNTIL, "0"))
            new = await self.store.messages_between(done_until, limit=MAX_MESSAGES_TO_READ)
            chat = [m for m in new if m.mode == "chat"]
            last_at = await self.store.get_setting(KEY_EVALUATED_AT)
            day_passed = last_at is None or now - datetime.fromisoformat(last_at) >= timedelta(days=1)
            if not force and len(chat) < EVALUATE_EVERY and not (day_passed and len(chat) >= MIN_MESSAGES_FOR_DAILY):
                return False
            if chat:
                try:
                    await self._evaluate(chat, now)
                except (LLMError, ValueError) as e:
                    logger.warning("Оценка отношений не удалась: %s", e)
                    return False
            if new:
                await self.store.set_setting(KEY_EVALUATED_UNTIL, str(new[-1].id))
            await self.store.set_setting(KEY_EVALUATED_AT, now.isoformat())
            return bool(chat)

    async def _evaluate(self, messages, now: datetime) -> None:
        stage = await self.stage()
        system = EVALUATE_SYSTEM.format(stage=stage_label(stage))
        result = await self.router.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": format_messages(messages)}],
            purpose="service", temperature=0.2, max_tokens=300,
        )
        data = parse_json(result.text)
        try:
            delta = float(data.get("delta", 0))
        except (TypeError, ValueError):
            delta = 0.0
        reason = str(data.get("reason", "")).strip()[:300]
        level = await self.apply(delta, reason, bool(data.get("romantic_interest")), now)
        logger.info("Отношения: %+.0f (%s) → уровень %.1f", delta, reason, level)
