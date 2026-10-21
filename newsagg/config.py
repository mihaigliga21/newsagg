"""YAML config, validated with pydantic. ${VAR} and ${VAR:-default} are read from the environment."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(value):
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


class ChannelConfig(BaseModel):
    bot_token: str
    chat_id: str
    review_chat_id: str | None = None
    language: str = "ro"
    timezone: str = "Europe/Chisinau"
    schedule: list[str] = Field(default_factory=lambda: ["0 20 * * *"])
    header: str | None = "📰 <b>Știrile zilei, {date}</b>"
    lookback_hours: int = 24


class SectionConfig(BaseModel):
    id: str
    title: str | None = None
    regions: list[str]
    max_items: int = 8


class RegionConfig(BaseModel):
    description: str = ""
    keywords: list[str] = Field(default_factory=list)


class TopicConfig(BaseModel):
    emoji: str
    keywords: list[str] = Field(default_factory=list)


class SourceConfig(BaseModel):
    id: str
    type: Literal["rss", "website", "telegram"]
    name: str | None = None
    url: str | None = None
    channel: str | None = None
    region: str | None = None
    priority: int = 5
    interval_minutes: int = 15
    enabled: bool = True
    link_selector: str | None = None
    max_items: int = 30

    @model_validator(mode="after")
    def _check(self):
        if self.type in ("rss", "website") and not self.url:
            raise ValueError(f"source {self.id}: 'url' is required for type {self.type}")
        if self.type == "website" and not self.link_selector:
            raise ValueError(f"source {self.id}: 'link_selector' is required for websites")
        if self.type == "telegram" and not self.channel:
            raise ValueError(f"source {self.id}: 'channel' is required for telegram sources")
        return self

    @property
    def display_name(self) -> str:
        return self.name or self.id


class FilterConfig(BaseModel):
    max_age_hours: int = 36
    blocklist: list[str] = Field(default_factory=list)


class DedupeConfig(BaseModel):
    window_hours: int = 48
    title_similarity: int = 85
    min_tokens: int = 4


class LLMConfig(BaseModel):
    enabled: bool = True
    provider: Literal["anthropic", "openai_compatible"] = "anthropic"
    model: str = "claude-haiku-5-5"
    api_key: str | None = None
    base_url: str | None = None
    effort: str | None = "low"
    classify: bool = True
    write_lines: bool = True
    batch_size: int = 20
    min_importance: int = 3


class WorkerConfig(BaseModel):
    poll_seconds: int = 60
    batch_size: int = 50


class Config(BaseModel):
    database_url: str
    channel: ChannelConfig
    sections: list[SectionConfig]
    regions: dict[str, RegionConfig]
    topics: dict[str, TopicConfig]
    sources: list[SourceConfig]
    filter: FilterConfig = FilterConfig()
    dedupe: DedupeConfig = DedupeConfig()
    llm: LLMConfig = LLMConfig()
    worker: WorkerConfig = WorkerConfig()

    @model_validator(mode="after")
    def _check_refs(self):
        known = set(self.regions)
        for s in self.sections:
            for r in s.regions:
                if r not in known:
                    raise ValueError(f"section {s.id}: unknown region {r!r}")
        for src in self.sources:
            if src.region and src.region not in known:
                raise ValueError(f"source {src.id}: unknown region {src.region!r}")
        ids = [s.id for s in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("source ids must be unique")
        if "default" not in self.topics:
            raise ValueError("topics must include a 'default' entry")
        return self

    def source(self, source_id: str) -> SourceConfig | None:
        return next((s for s in self.sources if s.id == source_id), None)

    def section_for_region(self, region: str | None) -> str | None:
        for s in self.sections:
            if region in s.regions:
                return s.id
        return None


def load_config(path: str | Path) -> Config:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return Config.model_validate(_expand_env(raw))
