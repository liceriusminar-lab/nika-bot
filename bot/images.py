"""Фото и картинки.

Мои фото описывает vision-модель. Свои картинки Ника «заказывает» меткой
[[картинка: тип — что на ней]] прямо в ответе — так работает с любой моделью, без function calling.
Рисование платное, поэтому при 80% бюджета — только дешёвая модель, при 100% — картинок нет."""

import base64
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from bot.config import Settings
from bot.llm.base import LLMError
from bot.llm.openai_compatible import OpenAICompatibleProvider
from bot.llm.refusal import looks_like_refusal
from bot.llm.router import LLMRouter
from bot.memory.store import Store

logger = logging.getLogger(__name__)

KEY_ENABLED = "images:enabled"

MARKER = re.compile(r"\[\[\s*(?:картинка|фото|рисунок|image)\s*:\s*(.+?)\s*\]\]", re.IGNORECASE | re.DOTALL)
# «рисунок — кот на подоконнике», «фото: кофе», «автопортрет | я в худи»
_KIND_PREFIX = re.compile(r"^\s*(рисунок|скетч|арт|фото|автопортрет|селфи)\s*[—–\-:|]\s*(.+)$",
                          re.IGNORECASE | re.DOTALL)

VISION_PROMPT = ("Опиши это фото по-русски, 2–4 предложения: что или кто на нём, обстановка, заметные детали, "
                 "надписи, если есть, и настроение. Только факты, без оценок и без обращения к собеседнику.")

# Если тип в метке не указан — угадываем по словам
_SELF = re.compile(r"автопортрет|селфи|\bсебя\b|\bсебе\b|\bменя\b|\bя (?:в|на|с)\b", re.IGNORECASE)
_DRAWING = re.compile(r"^(?:мой |свой )?(?:рисун|скетч|набросок|арт\b|иллюстрац|эскиз)", re.IGNORECASE)
_PEOPLE = re.compile(r"девушк|парен|человек|люди|подруг|соседк|\bя\b", re.IGNORECASE)

FALLBACK_TEXT = "блин, телефон тупит(( потом скину"


def extract_marker(text: str) -> tuple[str, str | None]:
    """Вырезает из ответа метку картинки. (текст без метки, описание картинки или None)."""
    match = MARKER.search(text)
    if not match:
        return text, None
    clean = MARKER.sub("", text)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean, match.group(1).strip()


def image_kind(scene: str) -> tuple[str, str]:
    """(тип: drawing | photo | self, описание без типа)."""
    match = _KIND_PREFIX.match(scene)
    if match:
        word, rest = match.group(1).lower(), match.group(2).strip()
        kind = "self" if word in ("автопортрет", "селфи") else ("photo" if word == "фото" else "drawing")
        return kind, rest
    if _SELF.search(scene):
        return "self", scene
    if _DRAWING.search(scene):
        return "drawing", scene
    return "photo", scene


def build_image_prompt(scene: str, appearance_prompt: str) -> tuple[str, str]:
    """Задание для генератора и соотношение сторон: сцена + стиль (+ внешность Ники, если она на картинке)."""
    appearance = " ".join(appearance_prompt.split())
    kind, scene = image_kind(scene)
    if kind == "self":
        style = ("Скетч-автопортрет в стиле цифровой иллюстрации, мягкие линии и цвета, "
                 f"как в скетчбуке студентки-иллюстратора. Внешность: {appearance}")
        ratio = "3:4"
    elif kind == "drawing":
        style = ("Цифровая иллюстрация или скетч из скетчбука студентки-иллюстратора, "
                 "авторский уютный стиль, мягкие цвета")
        if _PEOPLE.search(scene):
            style += f". Если на рисунке сама художница, её внешность: {appearance}"
        ratio = "1:1"
    else:
        style = "Любительское фото на телефон, реалистично, естественный свет, уютная повседневная атмосфера"
        if not _PEOPLE.search(scene):
            style += ", людей в кадре нет"
        ratio = "3:4"
    prompt = (f"{scene}. {style}. Без текста, подписей и надписей на изображении. "
              f"Повседневная, приличная сцена.")
    return prompt, ratio


class ImageService:
    def __init__(self, settings: Settings, persona: dict[str, Any], store: Store, router: LLMRouter,
                 provider: OpenAICompatibleProvider, now: Callable[[], datetime]) -> None:
        self.settings = settings
        self.persona = persona
        self.store = store
        self.router = router
        self.provider = provider
        self.now = now
        self.images_dir = settings.data_dir / "images"

    # ---------- Мои фото ----------

    async def describe_photo(self, image: bytes, caption: str | None) -> str | None:
        """Описание фото текстом. None — если модель не справилась."""
        model = self.settings.vision_model
        if not model:
            return None
        prompt = VISION_PROMPT + (f"\nПодпись к фото: «{caption}»" if caption else "")
        data_url = "data:image/jpeg;base64," + base64.b64encode(image).decode()
        try:
            result = await self.router.chat(
                [{"role": "user", "content": [{"type": "text", "text": prompt},
                                              {"type": "image_url", "image_url": {"url": data_url}}]}],
                purpose="service", temperature=0.3, max_tokens=400, model=model,
            )
        except LLMError as e:
            logger.warning("Фото не описано: %s", e)
            return None
        text = result.text.strip()
        return text if text and not looks_like_refusal(text) else None

    # ---------- Её картинки ----------

    async def enabled(self) -> bool:
        default = "1" if self.settings.images_enabled else "0"
        return bool(self.settings.image_models_list) and await self.store.get_setting(KEY_ENABLED, default) == "1"

    def _counter_key(self) -> str:
        return f"images:sent:{self.now().date().isoformat()}"

    async def left_today(self) -> int:
        sent = int(await self.store.get_setting(self._counter_key(), "0"))
        return max(0, self.settings.max_images_per_day - sent)

    async def _models(self) -> list[str]:
        """Какими моделями рисовать с учётом бюджета: обычные, только дешёвая или никакими."""
        models = self.settings.image_models_list
        budget = self.settings.monthly_budget_rub
        if budget > 0 and models:
            spent = await self.router.month_spent()
            if spent >= budget:
                return []
            if spent >= 0.8 * budget:
                return models[-1:]  # последняя в списке — самая дешёвая
        return models

    async def can_send(self) -> bool:
        return await self.enabled() and await self.left_today() > 0 and bool(await self._models())

    async def availability_note(self) -> str:
        """Подсказка для основной модели: можно ли сейчас прислать картинку и как."""
        if await self.can_send():
            return ("Картинки: если он просит показать или тебе правда хочется поделиться, можешь приложить "
                    "картинку. Для этого добавь в самом конце ответа отдельной строкой метку "
                    "[[картинка: тип — что на ней, коротко]], где тип — «рисунок» (твой рисунок или скетч), "
                    "«фото» (то, что вокруг: кофе, вид из окна, Соня) или «автопортрет» (ты сама, только "
                    "скетчем). Не в каждом сообщении — редко и к месту. Только повседневные и приличные "
                    "картинки; откровенных фото не бывает — на такие просьбы отшучивайся.")
        return ("Картинки сегодня не отправляй и метку [[картинка: ...]] не пиши. Если он просит фото — "
                "придумай естественную причину (телефон сел, потом скину).")

    async def generate(self, scene: str) -> bytes | None:
        """Рисует картинку по описанию из метки. None — если не вышло или нельзя (лимит, бюджет)."""
        if not await self.enabled() or await self.left_today() <= 0:
            return None
        prompt, ratio = build_image_prompt(scene, self.persona.get("appearance_prompt", ""))
        for model in await self._models():
            try:
                image, cost = await self.provider.generate_image(prompt, model, aspect_ratio=ratio)
            except LLMError as e:
                logger.warning("Картинка не получилась у %s: %s", model, e)
                continue
            await self.store.add_usage(self.now().date().isoformat(), model, 0, 0, 0, cost)
            key = self._counter_key()
            await self.store.set_setting(key, str(int(await self.store.get_setting(key, "0")) + 1))
            self._save(image)
            logger.info("Нарисована картинка (%s, %.2f ₽): %s", model, cost, scene)
            return image
        return None

    def _save(self, image: bytes) -> Path:
        """Её картинки сохраняются в data/images — на память."""
        self.images_dir.mkdir(parents=True, exist_ok=True)
        path = self.images_dir / f"{self.now():%Y%m%d_%H%M%S}.jpg"
        path.write_bytes(image)
        return path
