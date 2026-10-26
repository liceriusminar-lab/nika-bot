"""Любой OpenAI-совместимый API (Polza, VseGPT, Ollama…)."""

import asyncio
import logging

import httpx
import openai
from openai import AsyncOpenAI

from bot.llm.base import ChatProvider, ChatResult, LLMError, Prices

logger = logging.getLogger(__name__)


def _error_kind(e: openai.APIError) -> str:
    """Переводит ошибку библиотеки openai в наш LLMError.kind."""
    if isinstance(e, (openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError)):
        return "network"
    if isinstance(e, openai.APIStatusError) and e.status_code == 402:
        return "no_money"  # у Polza 402 = недостаточно средств или исчерпан лимит расходов
    return "other"


class OpenAICompatibleProvider(ChatProvider):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 90.0,
        disable_reasoning: bool = True,
    ) -> None:
        if not base_url or not api_key:
            raise LLMError("Не заданы адрес API или ключ (CHAT_BASE_URL / CHAT_API_KEY в .env)")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        # max_retries=2 — библиотека сама повторит запрос при сетевых сбоях и ошибках 5xx
        self.client = AsyncOpenAI(base_url=self.base_url, api_key=api_key, timeout=timeout, max_retries=2)
        # Многие модели (DeepSeek V4 и др.) сначала «думают» про себя: это медленно, дорого
        # и съедает лимит длины — ответ приходит пустым. Для переписки «размышления» не нужны.
        self.extra_body = {"reasoning": {"enabled": False}} if disable_reasoning else None

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        *,
        temperature: float = 0.9,
        max_tokens: int = 500,
    ) -> ChatResult:
        try:
            response = await self.client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                extra_body=self.extra_body,
            )
        except openai.APIError as e:
            raise LLMError(f"{model}: {e}", kind=_error_kind(e)) from e

        if not response.choices:
            raise LLMError(f"{model}: пустой ответ")

        choice = response.choices[0]
        text = choice.message.content or ""
        if not text.strip() and choice.finish_reason == "length":
            raise LLMError(f"{model}: ответ оборвался, не начавшись (модель потратила лимит на «размышления»)")

        usage = response.usage
        cached = 0
        if usage and usage.prompt_tokens_details and usage.prompt_tokens_details.cached_tokens:
            cached = usage.prompt_tokens_details.cached_tokens

        return ChatResult(
            text=text.strip(),
            model=model,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            cached_tokens=cached,
        )

    async def generate_image(self, prompt: str, model: str, aspect_ratio: str = "1:1",
                             timeout: float = 180.0) -> tuple[bytes, float]:
        """Рисует картинку. Возвращает (байты картинки, цена в рублях).

        Используется общий для всех моделей рисования эндпоинт Polza POST /v1/media:
        задача ставится в очередь, потом статус опрашивается (GET /v1/media/{id}), пока
        картинка не будет готова. Так умеют все модели, в том числе те, что не понимают
        OpenAI-шный /images/generations (z-image и др.). У других агрегаторов этого нет.
        """
        root = self.base_url.removesuffix("/v1")
        headers = {"Authorization": f"Bearer {self.api_key}"}
        body = {"model": model,
                "input": {"prompt": prompt, "aspect_ratio": aspect_ratio, "output_format": "jpeg"}}
        try:
            async with httpx.AsyncClient(timeout=60) as http:
                r = await http.post(f"{root}/v1/media", json=body, headers=headers)
                if r.status_code != 200:
                    kind = "no_money" if r.status_code == 402 else ("network" if r.status_code >= 500 else "other")
                    raise LLMError(f"{model}: {r.status_code} {r.text[:300]}", kind=kind)
                task = r.json()

                deadline = asyncio.get_running_loop().time() + timeout
                while task.get("status") not in ("completed", "failed"):
                    if asyncio.get_running_loop().time() > deadline:
                        raise LLMError(f"{model}: картинка не готова за {timeout:.0f} с", kind="network")
                    await asyncio.sleep(3)
                    r = await http.get(f"{root}/v1/media/{task['id']}", headers=headers)
                    if r.status_code == 200:
                        task = r.json()
                if task["status"] == "failed":
                    raise LLMError(f"{model}: генерация не удалась: {task.get('error')}")

                data = task.get("data") or {}
                item = data[0] if isinstance(data, list) else data
                if not item.get("url"):
                    raise LLMError(f"{model}: в ответе нет картинки")
                image = await http.get(item["url"])
                image.raise_for_status()
        except httpx.HTTPError as e:
            raise LLMError(f"{model}: сеть: {e}", kind="network") from e
        return image.content, float((task.get("usage") or {}).get("cost_rub") or 0)

    async def ping(self) -> bool:
        """Лёгкий запрос списка моделей — проверка, что сервис отвечает."""
        try:
            await self.client.models.list()
            return True
        except openai.APIError:
            return False

    async def get_prices(self) -> dict[str, Prices]:
        """Цены из каталога. Polza отдаёт их в /models; у других агрегаторов может не быть — тогда пусто."""
        prices: dict[str, Prices] = {}
        try:
            models = await self.client.models.list()
        except openai.APIError as e:
            logger.warning("Не удалось получить цены моделей: %s", e)
            return prices
        for m in models.data:
            p = ((m.model_extra or {}).get("top_provider") or {}).get("pricing") or {}
            try:
                cached = p.get("input_cache_read_per_million")
                prices[m.id] = Prices(
                    input=float(p["prompt_per_million"]),
                    output=float(p["completion_per_million"]),
                    cached_input=float(cached) if cached else None,
                )
            except (KeyError, TypeError, ValueError):
                continue
        return prices

    async def get_balance(self) -> float | None:
        """Остаток на счёте. Так умеет Polza (GET /api/v2/balance); у других — None."""
        root = self.base_url.removesuffix("/v1")
        try:
            async with httpx.AsyncClient(timeout=20) as http:
                r = await http.get(f"{root}/v2/balance", headers={"Authorization": f"Bearer {self.api_key}"})
            if r.status_code != 200:
                return None
            return float(r.json()["available"])
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return None
