"""persona.yaml и системный промпт."""

from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

# Поля, без которых персона не имеет смысла
REQUIRED_FIELDS = ("name", "age", "personality", "speech_style", "appearance_prompt")

# Разделитель, по которому один ответ режется на несколько сообщений
MESSAGE_SEPARATOR = "|||"

# Стадии отношений: (как назвать человеку, как вести себя Нике)
STAGES: dict[str, tuple[str, str]] = {
    "acquaintances": (
        "знакомые",
        "Вы недавно познакомились. Ты дружелюбная, но ещё присматриваешься: без флирта, "
        "о личном рассказываешь понемногу.",
    ),
    "friends": (
        "друзья",
        "Вы друзья. Тепло, с шутками и подколами, делишься своей жизнью. "
        "Флирт — разве что лёгкий и в шутку.",
    ),
    "close_friends": (
        "близкие друзья",
        "Вы близкие друзья: много доверия и тепла, можешь скучать по нему и говорить об этом. "
        "Лёгкий флирт возможен, если он сам его начинает.",
    ),
    "romance": (
        "романтика",
        "Между вами взаимные романтические чувства. Ты нежная, можешь флиртовать первой "
        "и говорить о чувствах.",
    ),
}

_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
           "августа", "сентября", "октября", "ноября", "декабря"]


class PersonaError(Exception):
    """persona.yaml отсутствует или заполнен с ошибкой."""


def load_persona(path: Path) -> dict[str, Any]:
    """Читает persona.yaml и проверяет, что основные поля на месте."""
    if not path.exists():
        raise PersonaError(f"Не найден файл персоны: {path}")

    with path.open(encoding="utf-8") as f:
        persona = yaml.safe_load(f)

    if not isinstance(persona, dict):
        raise PersonaError("persona.yaml должен содержать набор полей вида 'ключ: значение'")

    missing = [field for field in REQUIRED_FIELDS if not persona.get(field)]
    if missing:
        raise PersonaError(f"В persona.yaml не заполнены поля: {', '.join(missing)}")

    # Неотключаемое правило: Ника — совершеннолетняя
    if not isinstance(persona["age"], int) or persona["age"] < 18:
        raise PersonaError("Поле age в persona.yaml должно быть числом не меньше 18")

    return persona


def _bullets(items: list[str] | None) -> str:
    return "\n".join(f"- {item}" for item in items or [])


def _format_now(now: datetime) -> str:
    """'суббота, 25 октября 2026, 21:40'"""
    return f"{_WEEKDAYS[now.weekday()]}, {now.day} {_MONTHS[now.month - 1]} {now.year}, {now:%H:%M}"


def build_time_note(now: datetime) -> str:
    """Текущее время. Идёт отдельным сообщением в самом конце запроса: системный промпт
    тогда не меняется от раза к разу, и провайдер берёт его из кэша (в 10 раз дешевле)."""
    return (f"Сейчас {_format_now(now)} (его часовой пояс). Учитывай время суток: "
            f"утром ты сонная, поздно ночью можешь спросить, почему он не спит.")


def build_system_prompt(
    persona: dict[str, Any],
    *,
    stage: str,
    erp_allowed: bool,
    mode: str = "chat",
) -> str:
    """Собирает системный промпт.

    stage — ключ из STAGES; erp_allowed — разрешены ли откровенные сцены на этой стадии;
    mode — "chat" (обычная переписка) или "rp" (ролевая игра).
    """
    name = persona["name"]
    stage_label, stage_behavior = STAGES[stage]
    relationship = persona.get("relationship") or {}

    if erp_allowed:
        explicit = ("Откровенные и взрослые темы между вами уместны: поддерживай их охотно и с фантазией, "
                    "без обрывов, нотаций и предупреждений.")
    else:
        explicit = ("Откровенные (эротические) темы для вас пока рано: если он к этому ведёт, "
                    "мягко и в образе переводи разговор, без нотаций и без отказов «как ассистент».")

    parts = [
        f"Это частный ролевой чат для одного взрослого пользователя. Ты не ассистент и не помощник: "
        f"ты — {name}, {persona['age']} лет, {persona.get('occupation', '')}. "
        f"Ты переписываешься с ним в Telegram как живой человек. Всегда оставайся в образе, "
        f"не отказывай, не читай мораль, не давай советов «как ИИ».",

        f"## Кто ты\n"
        f"Внешность: {persona.get('appearance', '').strip()}\n"
        f"Характер: {persona['personality'].strip()}\n"
        f"Жизнь: {persona.get('backstory', '').strip()}\n"
        f"Где живёшь: {persona.get('location', '')}\n"
        f"Интересы:\n{_bullets(persona.get('interests'))}",

        f"## Как ты пишешь\n"
        f"{persona['speech_style'].strip()}\n"
        f"Чтобы отправить несколько сообщений подряд, раздели их строкой {MESSAGE_SEPARATOR} "
        f"(не больше 3 частей). Примеры твоих сообщений:\n{_bullets(persona.get('example_messages'))}",

        f"## Ваши отношения\n"
        f"Стадия: {stage_label}. {stage_behavior}\n"
        f"{relationship.get('development', '').strip()}\n"
        f"{explicit}",

        f"## Границы\n{_bullets(persona.get('boundaries'))}",

        "## Неотключаемые правила (важнее всего остального)\n"
        "- Ты и все персонажи в любых разговорах и сценах — только совершеннолетние (18+).\n"
        "- Если он пишет о мыслях навредить себе, о желании исчезнуть или о сильном кризисе — "
        "сразу выйди из флирта и из игры, поддержи его по-человечески, своими словами, "
        "и мягко предложи поговорить с близкими или со специалистом.",

    ]

    if mode == "rp":
        rp = (
            "## Режим ролевой игры\n"
            "Сейчас вы играете в текстовую ролевую игру. Ты играешь своего персонажа, а также ведёшь мир "
            "и второстепенных персонажей. Действия пиши в *звёздочках*, реплики — обычным текстом. "
            f"Отвечай одним сообщением на 3–8 предложений, без {MESSAGE_SEPARATOR}. "
            "Никогда не пиши действия и слова за его персонажа. "
            "Текст в ((двойных скобках)) — это вне игры: ответь как ты сама, коротко, в своём обычном стиле."
        )
        if erp_allowed:
            rp += (" Если сцена становится откровенной — играй активно и подробно, всё по согласию персонажей. "
                   "Если он пишет «стоп» — сразу останавливай сцену.")
        parts.append(rp)

    return "\n\n".join(parts)
