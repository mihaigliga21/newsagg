"""LLM providers behind one small interface.

To add a provider: subclass `LLMProvider`, implement `generate`, and register it in `build_provider`.
"""

from .base import LLMError, LLMProvider


def build_provider(cfg) -> LLMProvider:
    if cfg.provider == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(cfg)
    if cfg.provider == "openai_compatible":
        from .openai_compatible import OpenAICompatibleProvider

        return OpenAICompatibleProvider(cfg)
    raise ValueError(f"unknown LLM provider {cfg.provider!r}")


__all__ = ["LLMError", "LLMProvider", "build_provider"]
