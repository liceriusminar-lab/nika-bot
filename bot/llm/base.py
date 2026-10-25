"""Общие интерфейсы для работы с нейросетями.

Бот обращается к нейросетям только через эти классы, поэтому провайдера
можно заменить (другой агрегатор, локальная модель), не трогая остальной код.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


class LLMError(Exception):
    """Нейросеть не ответила.

    kind — что случилось, от этого зависит реакция роутера:
      "network"  — сеть, таймаут, сервис упал (5xx): провайдер недоступен;
      "no_money" — кончились деньги или лимит расходов;
      "other"    — остальное (неверный ID модели, слишком длинный запрос...).
    """

    def __init__(self, message: str, kind: str = "other") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class ChatResult:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    # Сколько входных токенов взято из кэша провайдера (они сильно дешевле)
    cached_tokens: int = 0


@dataclass
class Prices:
    """Цены модели в рублях за 1 млн токенов."""

    input: float
    output: float
    cached_input: float | None = None  # если None — считаем как обычный вход

    def cost(self, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
        cached_price = self.input if self.cached_input is None else self.cached_input
        return ((input_tokens - cached_tokens) * self.input
                + cached_tokens * cached_price
                + output_tokens * self.output) / 1_000_000


class ChatProvider(ABC):
    """Разговорная модель: получает историю сообщений, возвращает ответ."""

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        *,
        temperature: float = 0.9,
        max_tokens: int = 500,
    ) -> ChatResult:
        """messages — список вида [{"role": "system"|"user"|"assistant", "content": "..."}]."""

    async def ping(self) -> bool:
        """Жив ли провайдер. По умолчанию считаем, что да."""
        return True

    async def get_prices(self) -> dict[str, Prices]:
        """Цены моделей из каталога провайдера, если он их отдаёт."""
        return {}

    async def get_balance(self) -> float | None:
        """Остаток на счёте в рублях, если провайдер умеет это сообщать."""
        return None
