"""Извлечение памяти из переписки и поиск по фактам.

Факты, её «жизнь» и followups достаются одним запросом к дешёвой модели — так дешевле."""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from bot.llm.router import LLMRouter
from bot.memory.store import Fact, Store, StoredMessage

logger = logging.getLogger(__name__)

CATEGORIES = ("обо_мне", "предпочтения", "люди", "события", "наши_моменты")
CATEGORY_TITLES = {
    "обо_мне": "о тебе", "предпочтения": "любишь/не любишь", "люди": "люди",
    "события": "события", "наши_моменты": "наши моменты",
}
MAX_FACTS_IN_PROMPT = 200  # сколько уже известных фактов показывать модели, чтобы не плодить дубли

EXTRACT_SYSTEM = """Ты — модуль памяти чат-бота. Девушка Ника переписывается с парнем. Прочитай НОВЫЕ сообщения и верни JSON.

Что извлечь:
1. facts_new — новые факты о НЁМ (о парне), которые стоит помнить надолго: имя, работа/учёба, город, близкие люди и питомцы (с именами), привычки, вкусы, планы, важные события, а также важные моменты ваших отношений. Пиши коротко, в третьем лице: «Его зовут Влад», «Работает инженером, часто задерживается». Не записывай сиюминутное («сейчас устал», «пьёт чай») и то, что уже есть в известных фактах.
   category — одно из: обо_мне (о нём самом), предпочтения (что любит/не любит), люди (его близкие, друзья, коллеги, питомцы), события (что с ним происходило или будет), наши_моменты (важное между ним и Никой).
   importance — от 1 до 5: 5 — основа (имя, работа, самые близкие люди, очень важные события), 3 — полезная подробность, 1 — мелочь.
2. facts_update — если новое сообщение уточняет, дополняет или меняет ИЗВЕСТНЫЙ факт (про ту же самую вещь): {"id": номер, "text": новый полный текст, "importance": 1-5}. Всегда используй это вместо второго похожего факта.
3. her_life — что Ника рассказала О СЕБЕ и своей жизни: события («дорисовала заказ», «Соня уронила кружку»), новые подробности («подругу зовут Катя», «любимое аниме — Мононокэ»), мнения. Список строк, коротко, от третьего лица: «Ника дорисовала заказ для кафе». Не повторяй уже записанное и очевидное («Ника представилась»).
4. followups — будущие события в ЕГО сообщениях, о которых естественно потом спросить: {"text": "спросить, как прошло собеседование", "due": "YYYY-MM-DD HH:MM"} — due это когда уместно спросить (после события). Если время события неясно — ставь вечер того дня.

Отвечай ТОЛЬКО JSON без пояснений:
{"facts_new": [{"category": "...", "text": "...", "importance": 3}], "facts_update": [], "her_life": [], "followups": []}
Если извлекать нечего — верни пустые списки."""


@dataclass
class ExtractionStats:
    new_facts: int = 0
    updated_facts: int = 0
    her_events: int = 0
    followups: int = 0


# ---------- Поиск ----------

_STOP_WORDS = {
    "это", "что", "как", "так", "там", "тут", "мне", "меня", "тебя", "тебе", "ты", "был", "была", "было",
    "есть", "нет", "ещё", "еще", "уже", "очень", "просто", "если", "когда", "только", "вообще", "тоже",
    "или", "для", "его", "она", "они", "мой", "моя", "твой", "твоя", "сейчас", "потом", "ахах", "ахаха",
    "блин", "капец", "норм", "ладно", "давай", "кстати", "чем", "вот", "ну", "да", "ага",
}


def build_match_query(text: str, max_words: int = 12) -> str:
    """Превращает фразу в запрос FTS5: значимые слова с обрезанными окончаниями через OR.

    У FTS5 нет русской морфологии, поэтому «собеседование»/«собеседовании» ищем
    по началу слова: «собеседован*».
    """
    words = re.findall(r"[а-яёa-z0-9]+", text.lower())
    stems = []
    for w in words:
        if len(w) < 3 or w in _STOP_WORDS:
            continue
        stem = w[:-2] if len(w) > 5 else w   # грубо отрезаем окончание
        stem = stem[:12]
        if stem not in stems:
            stems.append(stem)
        if len(stems) >= max_words:
            break
    return " OR ".join(f'"{s}"*' for s in stems)


# ---------- Извлечение ----------

def format_messages(messages: list[StoredMessage]) -> str:
    return "\n".join(f"[{m.created_at:%d.%m %H:%M}] {'Он' if m.role == 'user' else 'Ника'}: {m.as_text()}"
                     for m in messages)


def parse_json(text: str) -> dict:
    """Достаёт JSON из ответа модели, даже если она обернула его в ```json ... ``` или добавила слова."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("в ответе нет JSON")
    return json.loads(match.group(0))


def _clamp_importance(value) -> int:
    try:
        return max(1, min(5, int(value)))
    except (TypeError, ValueError):
        return 3


def _normalize_category(value) -> str:
    value = str(value or "").strip().lower().replace(" ", "_")
    return value if value in CATEGORIES else "обо_мне"


def _parse_due(value, now: datetime) -> datetime | None:
    """'2026-10-27 15:00' или '2026-10-27' -> datetime в моём часовом поясе. Прошедшее — None."""
    value = str(value or "").strip()
    try:
        if len(value) <= 10:
            due = datetime.combine(datetime.fromisoformat(value).date(), time(19, 0))
        else:
            due = datetime.fromisoformat(value.replace(" ", "T"))
    except ValueError:
        return None
    if due.tzinfo is None:
        due = due.replace(tzinfo=now.tzinfo)
    return due if due > now - timedelta(hours=1) else None


async def extract_memory(
    router: LLMRouter,
    store: Store,
    new_messages: list[StoredMessage],
    context_before: list[StoredMessage],
    now: datetime,
) -> ExtractionStats:
    """Один запрос к дешёвой модели → новые факты, уточнения, её события, followups."""
    known_facts = (await store.all_facts())[-MAX_FACTS_IN_PROMPT:]
    known_her = await store.recent_her_events(40)

    parts = [f"Сегодня {now:%Y-%m-%d %H:%M}, {['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'][now.weekday()]}."]
    parts.append("Известные факты о нём:\n" + ("\n".join(
        f"{f.id}. [{f.category}, {f.importance}] {f.text}" for f in known_facts) or "пока нет"))
    parts.append("Уже записано о жизни Ники:\n" + ("\n".join(f"- {e.text}" for e in known_her) or "пока нет"))
    if context_before:
        parts.append("Предыдущие сообщения (только для контекста, из них НЕ извлекай):\n"
                     + format_messages(context_before))
    parts.append("НОВЫЕ сообщения:\n" + format_messages(new_messages))

    result = await router.chat(
        [{"role": "system", "content": EXTRACT_SYSTEM}, {"role": "user", "content": "\n\n".join(parts)}],
        purpose="service", temperature=0.2, max_tokens=1500,
    )
    data = parse_json(result.text)

    stats = ExtractionStats()
    known_texts = {f.text.casefold() for f in known_facts}
    known_ids = {f.id for f in known_facts}

    for item in data.get("facts_new") or []:
        text = str(item.get("text", "")).strip()
        if text and text.casefold() not in known_texts:
            await store.add_fact(_normalize_category(item.get("category")), text,
                                 _clamp_importance(item.get("importance")), now)
            known_texts.add(text.casefold())
            stats.new_facts += 1

    for item in data.get("facts_update") or []:
        try:
            fact_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        text = str(item.get("text", "")).strip()
        if fact_id in known_ids and text:
            await store.update_fact(fact_id, text, _clamp_importance(item.get("importance")))
            stats.updated_facts += 1

    known_her_texts = {e.text.casefold() for e in known_her}
    for item in data.get("her_life") or []:
        # Модель иногда присылает {"text": ...} вместо строки
        text = str(item.get("text", "") if isinstance(item, dict) else item).strip()
        if text and text.casefold() not in known_her_texts:
            await store.add_her_event(text, now)
            stats.her_events += 1

    for item in data.get("followups") or []:
        text = str(item.get("text", "")).strip()
        due = _parse_due(item.get("due"), now)
        if text and due:
            await store.add_followup(text, due, now)
            stats.followups += 1

    return stats


# ---------- Для контекста ----------

async def facts_for_context(store: Store, query: str, now: datetime) -> tuple[list[Fact], list[Fact]]:
    """(важные факты — всегда; факты, найденные по текущему сообщению — до 10 штук)."""
    important = await store.important_facts(min_importance=4, limit=40)
    relevant = await store.search_facts(build_match_query(query), limit=10,
                                        exclude_ids=[f.id for f in important])
    await store.touch_facts([f.id for f in relevant], now)
    return important, relevant
