"""Сводки старой переписки.

В запрос целиком идут только последние ~20 сообщений. Всё, что старше, дешёвая модель
сворачивает в краткую сводку. Сводка «накопительная»: новая включает в себя предыдущую,
поэтому в контекст всегда идёт одна — последняя.
"""

import re
from datetime import datetime

from bot.llm.router import LLMRouter
from bot.memory.facts import format_messages
from bot.memory.store import Store, StoredMessage

SUMMARY_SYSTEM = """Ты ведёшь краткий дневник переписки девушки Ники с парнем — её память о том, что было раньше.
Тебе дают прошлую сводку и следующие сообщения. Напиши ОБНОВЛЁННУЮ сводку, которая включает и старое, и новое.

Правила:
- СТРОГО не больше 150 слов, связный текст от третьего лица («Он рассказал…», «Ника пообещала…»);
- только главное: важные темы, что случилось у него и у неё, настроение, договорённости и обещания, шутки «для своих»;
- не пересказывай каждую реплику; старое сжимай сильнее, свежее — подробнее; указывай даты («25.10»);
- не выдумывай того, чего не было.
Ответь только текстом сводки, без вступлений вроде «Вот сводка»."""

# «Вот обновлённая сводка:» и подобные вступления в начале ответа
_PREAMBLE = re.compile(r"^\s*(?:вот\s+)?(?:обновл[её]нная\s+)?(?:кратк\w+\s+)?сводка\s*:?\s*\n*", re.IGNORECASE)


async def summarize(
    router: LLMRouter,
    store: Store,
    messages: list[StoredMessage],
    now: datetime,
    last_message_id: int | None = None,
) -> str:
    """Сворачивает messages вместе с предыдущей сводкой в новую сводку и сохраняет её.
    last_message_id — до какого сообщения считать свёрнутым (по умолчанию — последнее из messages)."""
    previous = await store.latest_summary()
    user = (f"Прошлая сводка:\n{previous.text if previous else 'пока нет'}\n\n"
            f"Следующие сообщения:\n{format_messages(messages)}")
    result = await router.chat(
        [{"role": "system", "content": SUMMARY_SYSTEM}, {"role": "user", "content": user}],
        purpose="service", temperature=0.3, max_tokens=1000,
    )
    text = _PREAMBLE.sub("", result.text).strip()
    if text:
        await store.add_summary(text, messages[0].created_at, messages[-1].created_at,
                                last_message_id or messages[-1].id, now)
    return text
