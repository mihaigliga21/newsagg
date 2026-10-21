from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class LLMProvider:
    """Returns a validated pydantic object for a system + user prompt."""

    def __init__(self, cfg):
        self.cfg = cfg

    async def generate(self, system: str, user: str, schema: type[T], max_tokens: int = 4000) -> T:
        raise NotImplementedError
