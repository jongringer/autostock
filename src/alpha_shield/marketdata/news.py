"""뉴스 헤드라인 가드용 공급자 — Yahoo Finance RSS (키 불필요)."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Sequence

from ..cache import TTLCache
from ..net import HttpTransport, TransportError

LOG = logging.getLogger(__name__)


class YahooHeadlineProvider:
    def __init__(
        self,
        transport: HttpTransport,
        feed_url: str,
        *,
        cache: TTLCache | None = None,
        cache_seconds: int = 300,
        limit: int = 20,
    ) -> None:
        self.transport = transport
        self.feed_url = feed_url
        self.cache = cache if cache is not None else TTLCache()
        self.cache_seconds = cache_seconds
        self.limit = limit

    def headlines(self, symbol: str) -> list[str] | None:
        key = f"news:{symbol}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached  # type: ignore[return-value]
        url = self.feed_url.format(symbol=symbol)
        try:
            response = self.transport.request("GET", url, headers={"Accept": "application/rss+xml,text/xml"})
        except TransportError as exc:
            LOG.warning("뉴스 RSS 실패 %s: %s", symbol, exc)
            return None
        if not response.ok:
            LOG.warning("뉴스 RSS 응답 %s (%s)", response.status, symbol)
            return None
        titles = self._parse_titles(response.text())
        self.cache.set(key, titles, self.cache_seconds)
        return titles

    def _parse_titles(self, xml_text: str) -> list[str]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            LOG.warning("뉴스 RSS 파싱 실패: %s", exc)
            return []
        titles: list[str] = []
        for item in root.iter():
            if item.tag.endswith("item"):
                for child in item:
                    if child.tag.endswith("title") and child.text:
                        titles.append(child.text.strip())
        return titles[: self.limit]


def match_keywords(titles: Sequence[str], keywords: Sequence[str]) -> list[str]:
    hits: list[str] = []
    for title in titles:
        lowered = title.lower()
        for keyword in keywords:
            if keyword in lowered:
                hits.append(f"{keyword}: {title}")
    return hits
