from __future__ import annotations

import anthropic

from .base import LLMError, LLMProvider, T


class AnthropicProvider(LLMProvider):
    def __init__(self, cfg):
        super().__init__(cfg)
        kwargs = {}
        if cfg.api_key:
            kwargs["api_key"] = cfg.api_key
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url
        self.client = anthropic.AsyncAnthropic(**kwargs)

    async def generate(self, system: str, user: str, schema: type[T], max_tokens: int = 4000) -> T:
        extra = {}
        if self.cfg.effort:
            extra["output_config"] = {"effort": self.cfg.effort}
        try:
            resp = await self.client.messages.parse(
                model=self.cfg.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=schema,
                **extra,
            )
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"Anthropic connection error: {exc}") from exc
        except (anthropic.AnthropicError, TypeError, ValueError) as exc:  # bad config or unparsable output
            raise LLMError(f"Anthropic call failed: {exc}") from exc
        if resp.stop_reason == "refusal":
            raise LLMError("model declined the request")
        if resp.stop_reason == "max_tokens":
            raise LLMError("response hit max_tokens; lower the batch size")
        if resp.parsed_output is None:
            raise LLMError("no structured output in response")
        return resp.parsed_output
