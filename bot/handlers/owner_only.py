"""Фильтр «только для меня».

Middleware — это прослойка, через которую проходит каждое входящее событие
до того, как оно попадёт в обработчики. Здесь мы просто выкидываем всё,
что пришло не от владельца: бот для чужих людей как будто не существует.
"""

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User

logger = logging.getLogger(__name__)


class OwnerOnlyMiddleware(BaseMiddleware):
    def __init__(self, owner_id: int) -> None:
        self.owner_id = owner_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # aiogram сам кладёт сюда автора события (если он есть)
        user: User | None = data.get("event_from_user")

        if user is None or user.id != self.owner_id:
            if user is not None:
                logger.info("Игнорирую сообщение от чужого пользователя id=%s", user.id)
            return None  # молча пропускаем, никакого ответа

        return await handler(event, data)
