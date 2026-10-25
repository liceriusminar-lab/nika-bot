"""Картинки: распознавание фото и рисование. Агрегатор подменён."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bot.config import PROJECT_ROOT
from bot.dialogue import Dialogue
from bot.images import ImageService, build_image_prompt, extract_marker, image_kind
from bot.llm.base import LLMError
from bot.persona import load_persona
from tests.helpers import ScriptedProvider, make_router, make_settings

TZ = ZoneInfo("Asia/Vladivostok")
NOW = datetime(2026, 10, 26, 15, 0, tzinfo=TZ)
PERSONA = load_persona(PROJECT_ROOT / "persona.yaml")


class FakeImageProvider(ScriptedProvider):
    """Агрегатор: отвечает на текстовые запросы по сценарию и «рисует» картинки."""

    def __init__(self, *answers: str, fail_models: tuple[str, ...] = ()) -> None:
        super().__init__(*answers)
        self.fail_models = fail_models
        self.drawn: list[tuple[str, str, str]] = []

    async def generate_image(self, prompt, model, aspect_ratio="1:1"):
        if model in self.fail_models:
            raise LLMError(f"{model} упал")
        self.drawn.append((model, prompt, aspect_ratio))
        return b"\xff\xd8jpeg", 3.0


def make_images(store, tmp_path, provider=None, **settings):
    provider = provider or FakeImageProvider()
    s = make_settings(data_dir=tmp_path, **settings)
    router = make_router(store, provider, **settings)
    return ImageService(s, PERSONA, store, router, provider, now=lambda: NOW), provider


# ---------- Метка и задание для генератора ----------

def test_extract_marker():
    text, scene = extract_marker("смотри что нарисовала)\n[[картинка: рисунок — кошка Соня на подоконнике]]")
    assert text == "смотри что нарисовала)" and scene == "рисунок — кошка Соня на подоконнике"
    assert extract_marker("просто текст") == ("просто текст", None)
    assert extract_marker("[[ Картинка : кофе ]]") == ("", "кофе")


def test_image_kind():
    assert image_kind("фото — кофе рядом со скетчбуком") == ("photo", "кофе рядом со скетчбуком")
    assert image_kind("рисунок: дракон") == ("drawing", "дракон")
    assert image_kind("автопортрет | я в новом худи") == ("self", "я в новом худи")
    assert image_kind("мой скетч дракона")[0] == "drawing"   # без типа — угадываем
    assert image_kind("кружка кофе рядом со скетчбуком")[0] == "photo"


def test_prompt_styles():
    appearance = PERSONA["appearance_prompt"]
    selfie, ratio = build_image_prompt("автопортрет — я в новом худи", appearance)
    assert "long wavy dark" in selfie and ratio == "3:4"
    coffee, _ = build_image_prompt("фото — кружка кофе с карамелью", appearance)
    assert "фото" in coffee and "людей в кадре нет" in coffee and "long wavy" not in coffee
    drawing, _ = build_image_prompt("рисунок — дракон", appearance)
    assert "скетч" in drawing and "long wavy" not in drawing


# ---------- Генерация, лимит и бюджет ----------

async def test_generate_respects_daily_limit_and_records_cost(store, tmp_path):
    images, provider = make_images(store, tmp_path, max_images_per_day=2)
    assert await images.generate("фото — кофе") == b"\xff\xd8jpeg"
    assert await images.generate("рисунок — кот") is not None
    assert await images.left_today() == 0
    assert await images.generate("ещё") is None            # лимит 2 в день
    assert [m for m, _, _ in provider.drawn] == ["bytedance/seedream-4"] * 2
    assert await images.router.month_spent() == pytest.approx(6.0)
    assert list((tmp_path / "images").glob("*.jpg"))
    assert "не отправляй" in await images.availability_note()


async def test_generate_falls_back_to_next_model(store, tmp_path):
    images, provider = make_images(store, tmp_path, FakeImageProvider(fail_models=("bytedance/seedream-4",)))
    assert await images.generate("фото — кофе") is not None
    assert provider.drawn[0][0] == "tongyi-mai/z-image"


async def test_generate_failure_does_not_spend_limit(store, tmp_path):
    provider = FakeImageProvider(fail_models=("bytedance/seedream-4", "tongyi-mai/z-image"))
    images, _ = make_images(store, tmp_path, provider)
    assert await images.generate("кофе") is None
    assert await images.left_today() == 2


async def test_budget_switches_to_cheap_then_disables(store, tmp_path):
    images, provider = make_images(store, tmp_path, monthly_budget_rub=10)
    await store.add_usage(NOW.date().isoformat(), "main", 0, 0, 0, cost_rub=8.5)   # 85% бюджета
    await images.generate("кофе")
    assert provider.drawn[-1][0] == "tongyi-mai/z-image"
    await store.add_usage(NOW.date().isoformat(), "main", 0, 0, 0, cost_rub=5)     # больше бюджета
    assert not await images.can_send()
    assert await images.generate("кофе") is None


async def test_disabled_in_settings(store, tmp_path):
    images, _ = make_images(store, tmp_path)
    await store.set_setting("images:enabled", "0")
    assert not await images.enabled() and await images.generate("кофе") is None


# ---------- Мои фото ----------

async def test_describe_photo(store, tmp_path):
    images, provider = make_images(store, tmp_path, FakeImageProvider("Рыжий кот на подоконнике."))
    assert await images.describe_photo(b"img", "мой кот") == "Рыжий кот на подоконнике."
    sent = provider.prompts[0][0]["content"]
    assert "мой кот" in sent[0]["text"]
    assert sent[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


async def test_describe_photo_refusal_is_none(store, tmp_path):
    images, _ = make_images(store, tmp_path, FakeImageProvider("I'm sorry, but I can't help with that."))
    assert await images.describe_photo(b"img", None) is None


# ---------- Картинки в истории ----------

async def test_photo_description_goes_to_model(store, tmp_path):
    await store.add_message("user", "смотри кто пришёл", NOW, has_image=True, image_description="рыжий кот")
    await store.add_message("assistant", "", NOW, has_image=True, image_description="рисунок — скетч Сони")
    await store.add_message("user", "", NOW, has_image=True, image_description=None)
    images, _ = make_images(store, tmp_path)
    dialogue = Dialogue(make_settings(), PERSONA, store, router=None, images=images)
    messages = await dialogue.build_messages(await store.recent_messages(10))
    assert messages[1]["content"] == "[он прислал фото: рыжий кот]\nсмотри кто пришёл"
    assert messages[2]["content"] == "[[картинка: рисунок — скетч Сони]]"
    assert messages[3]["content"].startswith("[он прислал фото, но оно у тебя не открылось]")
    assert "[[картинка: тип — что на ней, коротко]]" in messages[3]["content"]  # подсказка: можно прислать


async def test_generate_reply_strips_marker(store, tmp_path):
    router = make_router(store, ScriptedProvider("вот, держи)\n[[картинка: рисунок — дракон]]"))
    dialogue = Dialogue(make_settings(), PERSONA, store, router)
    await store.add_message("user", "покажи что рисуешь", NOW)
    reply = await dialogue.generate_reply()
    assert reply.text == "вот, держи)" and reply.image_scene == "рисунок — дракон"
    assert (await store.last_message()).content == "вот, держи)"
