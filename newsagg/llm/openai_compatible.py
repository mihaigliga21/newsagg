"""Any provider with an OpenAI-style /chat/completions endpoint: OpenAI, Gemini, Mistral, Groq, OpenRouter, Ollama.

Structured output is requested in strict mode, but not every provider (or OpenRouter route) enforces
it, so the reply is parsed leniently: JSON with small syntax slips (an unescaped quote inside a
string, a missing comma) is repaired, and a reply that still doesn't fit the schema is asked for again.
"""

from __future__ import annotations

import json
import logging

import httpx
import json_repair
from pydantic import ValidationError

from .base import LLMError, LLMProvider, T

log = logging.getLogger(__name__)

ATTEMPTS = 2  # a reply that is not valid JSON for the schema is requested once more


def _json_part(content: str) -> str:
    """Some providers ignore response_format and wrap the JSON in prose or a ```json fence."""
    start, end = content.find("{"), content.rfind("}")
    return content[start : end + 1] if 0 <= start < end else content


def _parse(content: str):
    text = _json_part(content)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        around = text[max(0, exc.pos - 80) : exc.pos + 80]
        log.warning("LLM reply is not valid JSON (%s), repairing it; near the error: %r", exc, around)
        return json_repair.loads(text)


def _usage(data: dict) -> str:
    """How the output tokens were spent, to tell a long answer from one eaten by reasoning."""
    usage = data.get("usage") or {}
    out = f"{usage.get('completion_tokens', '?')} output tokens"
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    return f"{out}, {reasoning} of them reasoning" if reasoning else out


def strict_schema(schema: dict) -> dict:
    """OpenAI strict mode wants every object closed and every property required."""
    if isinstance(schema, dict):
        schema = {k: strict_schema(v) for k, v in schema.items()}
        if schema.get("type") == "object" and "properties" in schema:
            schema["additionalProperties"] = False
            schema["required"] = list(schema["properties"])
    elif isinstance(schema, list):
        schema = [strict_schema(v) for v in schema]
    return schema


class OpenAICompatibleProvider(LLMProvider):
    def __init__(self, cfg):
        super().__init__(cfg)
        if not cfg.base_url:
            raise ValueError("llm.base_url is required for openai_compatible (e.g. https://api.openai.com/v1)")
        headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        self.http = httpx.AsyncClient(base_url=cfg.base_url.rstrip("/"), headers=headers, timeout=120)
        self.strict = True  # turned off if the provider rejects strict schemas

    def _body(self, system: str, user: str, schema: type[T], max_tokens: int) -> dict:
        json_schema = {"name": schema.__name__, "schema": schema.model_json_schema()}
        if self.strict:
            json_schema.update(strict=True, schema=strict_schema(json_schema["schema"]))
        return {
            "model": self.cfg.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema", "json_schema": json_schema},
        }

    async def generate(self, system: str, user: str, schema: type[T], max_tokens: int = 4000) -> T:
        for attempt in range(1, ATTEMPTS + 1):
            try:
                resp = await self.http.post("/chat/completions", json=self._body(system, user, schema, max_tokens))
                if resp.status_code == 400 and self.strict:
                    log.warning("provider rejected the strict schema (%s); using a plain one", resp.text[:300])
                    self.strict = False
                    resp = await self.http.post("/chat/completions", json=self._body(system, user, schema, max_tokens))
                resp.raise_for_status()
                data = resp.json()
                choice = data["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise LLMError(f"response hit max_tokens ({_usage(data)}); lower the batch size")
                return schema.model_validate(_parse(choice["message"]["content"]))
            except (json.JSONDecodeError, ValidationError) as exc:
                if attempt == ATTEMPTS:
                    raise LLMError(f"openai_compatible call failed: {exc}") from exc
                log.warning("LLM reply did not fit %s (%s); asking again", schema.__name__, exc)
            except (httpx.HTTPError, KeyError, TypeError) as exc:
                raise LLMError(f"openai_compatible call failed: {exc}") from exc
