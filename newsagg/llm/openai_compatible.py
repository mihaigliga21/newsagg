"""Any provider with an OpenAI-style /chat/completions endpoint: OpenAI, Gemini, Mistral, Groq, OpenRouter, Ollama."""

from __future__ import annotations

import json

import httpx
from pydantic import ValidationError

from .base import LLMError, LLMProvider, T


def _json_part(content: str) -> str:
    """Some providers ignore response_format and wrap the JSON in prose or a ```json fence."""
    start, end = content.find("{"), content.rfind("}")
    return content[start : end + 1] if 0 <= start < end else content


class OpenAICompatibleProvider(LLMProvider):
    def __init__(self, cfg):
        super().__init__(cfg)
        if not cfg.base_url:
            raise ValueError("llm.base_url is required for openai_compatible (e.g. https://api.openai.com/v1)")
        headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        self.http = httpx.AsyncClient(base_url=cfg.base_url.rstrip("/"), headers=headers, timeout=120)

    async def generate(self, system: str, user: str, schema: type[T], max_tokens: int = 4000) -> T:
        body = {
            "model": self.cfg.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()},
            },
        }
        try:
            resp = await self.http.post("/chat/completions", json=body)
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            return schema.model_validate(json.loads(_json_part(content)))
        except (httpx.HTTPError, KeyError, TypeError, json.JSONDecodeError, ValidationError) as exc:
            raise LLMError(f"openai_compatible call failed: {exc}") from exc
