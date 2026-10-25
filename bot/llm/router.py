"""Роутер нейросетей: через него идут все запросы к модели разговора.

Что он делает:
- выбирает модель: для разговора — CHAT_MODEL, для служебных задач — CHEAP_MODEL;
- если модель отказала или ответила «как ассистент» — пробует запасные (CHAT_FALLBACK_MODELS);
- если основной агрегатор 2 раза подряд недоступен или кончились деньги — переходит
  на резервный (CHAT_BACKUP_*), а раз в 10 минут проверяет, не ожил ли основной;
- считает токены и рубли, следит за бюджетом и балансом, присылает мне предупреждения;
- ограничивает число одновременных запросов (очередь).
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Awaitable, Callable

from bot.config import Settings
from bot.llm.base import ChatProvider, ChatResult, LLMError, Prices
from bot.llm.refusal import looks_like_refusal
from bot.memory.store import Store

logger = logging.getLogger(__name__)

Notify = Callable[[str], Awaitable[None]]

FAILURES_TO_SWITCH = 2          # сколько сбоев подряд до перехода на резерв
BACKUP_RECHECK_SECONDS = 600    # как часто проверять, ожил ли основной
BALANCE_CHECK_SECONDS = 1800    # как часто смотреть баланс
LOW_BALANCE_RUB = 30.0          # ниже этого — предупреждение
MAX_PARALLEL_REQUESTS = 2


class LLMRouter:
    def __init__(
        self,
        settings: Settings,
        primary: ChatProvider,
        store: Store,
        notify: Notify,
        backup: ChatProvider | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.primary = primary
        self.backup = backup if settings.chat_backup_model else None
        self.store = store
        self.notify = notify
        self.clock = clock

        self.prices: dict[str, Prices] = {}
        self.on_backup = False
        self._failures = 0
        self._last_primary_check = 0.0
        self._last_balance_check = -BALANCE_CHECK_SECONDS
        self._queue = asyncio.Semaphore(MAX_PARALLEL_REQUESTS)
        self._background: set[asyncio.Task] = set()

    # ---------- Подготовка ----------

    async def load_prices(self) -> None:
        """Берёт цены из каталогов провайдеров; для основной модели можно задать вручную в .env."""
        self.prices = await self.primary.get_prices()
        if self.backup is not None:
            for model, price in (await self.backup.get_prices()).items():
                self.prices.setdefault(model, price)
        s = self.settings
        if s.chat_model and s.chat_model not in self.prices and s.price_per_1m_input_rub:
            self.prices[s.chat_model] = Prices(s.price_per_1m_input_rub, s.price_per_1m_output_rub)
        for model in [s.chat_model, s.cheap_model, *s.chat_fallback_models_list]:
            if model and model not in self.prices:
                logger.warning("Нет цены для модели %s — её расходы не будут учтены", model)

    # ---------- Главный метод ----------

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str = "chat",
        temperature: float = 0.9,
        max_tokens: int = 500,
        model: str | None = None,
    ) -> ChatResult:
        """purpose: "chat" — ответ Ники (проверяется на отказы), "service" — служебная задача.
        model — принудительно эта модель основного агрегатора, без запасных (например, для фото)."""
        if model:
            async with self._queue:
                return await self._call(self.primary, model, messages, temperature, max_tokens)

        async with self._queue:
            await self._maybe_return_to_primary()
            if self.on_backup:
                return await self._call(self.backup, self.settings.chat_backup_model, messages,
                                        temperature, max_tokens)

            first_refusal: ChatResult | None = None
            last_error: LLMError | None = None

            for model in await self._models_for(purpose):
                try:
                    result = await self._call(self.primary, model, messages, temperature, max_tokens)
                except LLMError as e:
                    last_error = e
                    logger.warning("Модель %s не ответила (%s): %s", model, e.kind, e)
                    if e.kind in ("network", "no_money"):
                        self._failures += 1
                        if self.backup is not None and (e.kind == "no_money" or self._failures >= FAILURES_TO_SWITCH):
                            await self._switch_to_backup(e)
                            return await self._call(self.backup, self.settings.chat_backup_model, messages,
                                                    temperature, max_tokens)
                        if e.kind == "no_money":
                            await self.notify_once("no_money", "⚠️ на балансе агрегатора кончились деньги "
                                                    "(или сработал лимит ключа) — пополни, пожалуйста")
                            break  # остальные модели того же агрегатора тоже не ответят
                    continue

                self._failures = 0
                self._schedule(self._check_balance())
                if purpose == "chat" and looks_like_refusal(result.text):
                    logger.warning("Модель %s ответила отказом/как ассистент, пробую следующую", model)
                    first_refusal = first_refusal or result
                    continue
                return result

            if first_refusal is not None and first_refusal.text.strip():
                # Все модели «отказали» — лучше показать хоть что-то, чем молчать
                logger.warning("Все модели ответили как ассистент, отдаю первый ответ")
                return first_refusal
            raise last_error or LLMError("Ни одна модель не дала нормального ответа")

    # ---------- Выбор модели и бюджет ----------

    async def _models_for(self, purpose: str) -> list[str]:
        s = self.settings
        if purpose == "service":
            models = [s.cheap_model, s.chat_model]
        elif await self._budget_exceeded():
            models = [s.cheap_model, *s.chat_fallback_models_list]
        else:
            models = [s.chat_model, *s.chat_fallback_models_list]
        # Убираем пустые и повторы, сохраняя порядок
        return list(dict.fromkeys(m for m in models if m))

    async def month_spent(self) -> float:
        return await self.store.cost_since(self.today().replace(day=1).isoformat())

    async def _budget_exceeded(self) -> bool:
        budget = self.settings.monthly_budget_rub
        if budget <= 0:
            return False
        spent = await self.month_spent()
        month = self.today().strftime("%Y-%m")
        if spent >= budget:
            await self.notify_once(f"budget100:{month}",
                                    f"⚠️ расходы на разговор за месяц достигли бюджета ({spent:.0f} из {budget:.0f} ₽). "
                                    f"До конца месяца перехожу на дешёвую модель")
            return True
        if spent >= 0.8 * budget:
            await self.notify_once(f"budget80:{month}",
                                    f"ℹ️ потрачено {spent:.0f} из {budget:.0f} ₽ месячного бюджета (80%)")
        return False

    # ---------- Резервный провайдер ----------

    async def _switch_to_backup(self, error: LLMError) -> None:
        self.on_backup = True
        self._failures = 0
        self._last_primary_check = self.clock()
        reason = "кончились деньги" if error.kind == "no_money" else "не отвечает"
        logger.error("Основной агрегатор %s, перехожу на резервный: %s", reason, error)
        await self._safe_notify(f"⚠️ основной агрегатор {reason} — переключилась на резервный")

    async def _maybe_return_to_primary(self) -> None:
        if not self.on_backup or self.clock() - self._last_primary_check < BACKUP_RECHECK_SECONDS:
            return
        self._last_primary_check = self.clock()
        if await self.primary.ping():
            self.on_backup = False
            logger.info("Основной агрегатор снова отвечает")
            await self._safe_notify("✅ основной агрегатор снова работает, вернулась на него")

    # ---------- Учёт и баланс ----------

    async def _call(self, provider: ChatProvider, model: str, messages, temperature, max_tokens) -> ChatResult:
        result = await provider.chat(messages, model, temperature=temperature, max_tokens=max_tokens)
        price = self.prices.get(model)
        cost = price.cost(result.input_tokens, result.output_tokens, result.cached_tokens) if price else 0.0
        await self.store.add_usage(self.today().isoformat(), model, result.input_tokens,
                                   result.output_tokens, result.cached_tokens, cost)
        return result

    async def _check_balance(self) -> None:
        if self.clock() - self._last_balance_check < BALANCE_CHECK_SECONDS:
            return
        self._last_balance_check = self.clock()
        balance = await self.primary.get_balance()
        if balance is None:
            return
        if balance < LOW_BALANCE_RUB:
            await self.notify_once(f"low_balance:{self.today()}",
                                    f"ℹ️ на балансе агрегатора осталось {balance:.0f} ₽ — скоро надо пополнить")

    # ---------- Вспомогательное ----------

    def today(self):
        return datetime.now(self.settings.tz).date()

    async def notify_once(self, key: str, text: str) -> None:
        """Отправляет уведомление, только если с таким ключом ещё не отправляли."""
        flag = f"notified:{key}"
        if await self.store.get_setting(flag):
            return
        await self.store.set_setting(flag, "1")
        await self._safe_notify(text)

    async def _safe_notify(self, text: str) -> None:
        try:
            await self.notify(text)
        except Exception:  # noqa: BLE001 — уведомление не должно ронять ответ
            logger.exception("Не удалось отправить уведомление")

    def _schedule(self, coro) -> None:
        """Запускает задачу в фоне, не задерживая ответ."""
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
