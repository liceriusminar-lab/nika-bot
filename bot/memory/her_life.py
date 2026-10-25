"""«Жизнь» Ники: что она уже рассказывала о себе.

Записи появляются при извлечении памяти (facts.extract_memory). Здесь — выбор
того, что показать модели: последние события и те, что связаны с текущим разговором.
Так Ника помнит, что уже говорила, и не противоречит себе.
"""

from bot.memory.facts import build_match_query
from bot.memory.store import HerEvent, Store

RECENT_EVENTS = 5
RELEVANT_EVENTS = 5


async def her_life_for_context(store: Store, query: str) -> tuple[list[HerEvent], list[HerEvent]]:
    """(последние события; события, найденные по текущему сообщению)."""
    recent = await store.recent_her_events(RECENT_EVENTS)
    relevant = await store.search_her_events(build_match_query(query), limit=RELEVANT_EVENTS,
                                             exclude_ids=[e.id for e in recent])
    return recent, relevant


def format_events(events: list[HerEvent]) -> str:
    return "\n".join(f"- {e.created_at:%d.%m}: {e.text}" for e in events)
