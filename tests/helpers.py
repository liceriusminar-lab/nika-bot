"""Общие заготовки для тестов: настройки, подставные провайдеры нейросетей, роутер."""

from bot.config import Settings
from bot.llm.base import ChatProvider, ChatResult, LLMError
from bot.llm.router import LLMRouter


def make_settings(**overrides) -> Settings:
    values = {
        "telegram_bot_token": "123:abc", "owner_telegram_id": 1,
        "chat_model": "main", "cheap_model": "cheap", "chat_fallback_models": "fb1,fb2",
        "monthly_budget_rub": 300,
    } | overrides
    # _env_file=None — не читаем настоящий .env, чтобы тесты от него не зависели
    return Settings(_env_file=None, **values)


class FakeProvider(ChatProvider):
    """Для каждой модели — заранее заданный ответ или ошибка."""

    def __init__(self, answers: dict, alive: bool = True) -> None:
        self.answers = answers
        self.alive = alive
        self.calls: list[str] = []

    async def chat(self, messages, model, *, temperature=0.9, max_tokens=500) -> ChatResult:
        self.calls.append(model)
        answer = self.answers.get(model, "ок)")
        if isinstance(answer, LLMError):
            raise answer
        return ChatResult(answer, model, input_tokens=1000, output_tokens=100, cached_tokens=500)

    async def ping(self) -> bool:
        return self.alive


class ScriptedProvider(ChatProvider):
    """Отвечает по очереди заданными текстами и запоминает, что у него спросили."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: list[list[dict]] = []

    async def chat(self, messages, model, *, temperature=0.9, max_tokens=500) -> ChatResult:
        self.prompts.append(messages)
        return ChatResult(self.answers.pop(0) if self.answers else "{}", model)


class Clock:
    """Подставные часы для роутера: время двигается вручную."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def make_router(store, primary, backup=None, notes=None, clock=None, **settings) -> LLMRouter:
    async def notify(text):
        if notes is not None:
            notes.append(text)
    return LLMRouter(make_settings(**settings), primary, store, notify, backup=backup, clock=clock or Clock())
