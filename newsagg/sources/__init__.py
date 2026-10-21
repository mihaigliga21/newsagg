from .base import FetchedItem, FetchResult, Source
from .rss import RssSource
from .telegram_web import TelegramWebSource
from .website import WebsiteSource

_TYPES = {"rss": RssSource, "website": WebsiteSource, "telegram": TelegramWebSource}


def build_source(cfg, http) -> Source:
    return _TYPES[cfg.type](cfg, http)


__all__ = ["FetchedItem", "FetchResult", "Source", "build_source"]
