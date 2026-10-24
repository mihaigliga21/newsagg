"""A local model served by Ollama, through its native /api/chat endpoint.

Ollama also serves an OpenAI-style /v1 API, but that one cannot set the context window: Ollama's
default (2-4k tokens) silently cuts the labelling and grouping prompts, which run to about 10k
tokens. /api/chat takes `num_ctx`, turns thinking off for reasoning models (`think`), and
constrains decoding to the JSON schema (`format`), so a reply is always well-formed JSON.
"""

from __future__ import annotations

import json
import logging

from .base import LLMError, T
from .openai_compatible import OpenAICompatibleProvider

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://host.docker.internal:11434"  # Ollama on the machine running Docker


def inline_refs(schema: dict) -> dict:
    """Pydantic puts nested models in $defs; not every Ollama version follows $ref in `format`."""
    defs = schema.get("$defs", {})

    def resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(defs[node["$ref"].rsplit("/", 1)[-1]])
            return {k: resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


class OllamaProvider(OpenAICompatibleProvider):
    name = "ollama"
    default_timeout = 600  # local models on a modest GPU take minutes for a batch of 20 lines

    def __init__(self, cfg):
        super().__init__(cfg)
        # The same base URL as for the OpenAI-style API works: the /v1 suffix is dropped.
        url = str(self.http.base_url).rstrip("/")
        self.http.base_url = url.removesuffix("/v1")
        self.think = cfg.think  # None: the model's default; dropped if the model has no thinking mode

    def default_base_url(self) -> str:
        return DEFAULT_BASE_URL

    def _body(self, system: str, user: str, schema: type[T], max_tokens: int) -> dict:
        options = {"num_ctx": self.cfg.num_ctx, "num_predict": max_tokens}
        if self.cfg.temperature is not None:
            options["temperature"] = self.cfg.temperature
        json_schema = inline_refs(schema.model_json_schema())
        # `format` only constrains decoding; the model reads the field descriptions from the prompt.
        system = f"{system}\n\nReply with JSON that follows this schema:\n{json.dumps(json_schema, ensure_ascii=False)}"
        body = {
            "model": self.cfg.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "format": json_schema,
            "stream": False,
            "options": options,
        }
        if self.think is not None:
            body["think"] = self.think
        if self.cfg.keep_alive:
            body["keep_alive"] = self.cfg.keep_alive
        return body

    async def _complete(self, system: str, user: str, schema: type[T], max_tokens: int) -> str:
        resp = await self.http.post("/api/chat", json=self._body(system, user, schema, max_tokens))
        if resp.status_code == 400 and self.think is not None and "think" in resp.text:
            log.warning("model %s has no thinking mode (%s); not setting llm.think", self.cfg.model, resp.text[:200])
            self.think = None
            resp = await self.http.post("/api/chat", json=self._body(system, user, schema, max_tokens))
        if resp.status_code == 404 and "not found" in resp.text:
            raise LLMError(f"Ollama has no model {self.cfg.model!r}; run `ollama pull {self.cfg.model}`")
        resp.raise_for_status()
        data = resp.json()
        prompt_tokens, out_tokens = data.get("prompt_eval_count") or 0, data.get("eval_count") or 0
        log.debug("ollama %s: %d prompt tokens, %d output tokens, %.1fs", self.cfg.model, prompt_tokens,
                  out_tokens, (data.get("total_duration") or 0) / 1e9)
        if prompt_tokens >= self.cfg.num_ctx - 16:
            # Ollama cuts a prompt longer than the context window instead of failing.
            raise LLMError(f"prompt filled the whole context window ({prompt_tokens} tokens, "
                           f"llm.num_ctx {self.cfg.num_ctx}); raise llm.num_ctx or lower the batch size")
        if data.get("done_reason") == "length":
            limit = "llm.num_ctx" if prompt_tokens + out_tokens >= self.cfg.num_ctx - 16 else "max_tokens"
            raise LLMError(f"response hit {limit} ({prompt_tokens} prompt + {out_tokens} output tokens); "
                           "lower the batch size")
        return data["message"]["content"]
