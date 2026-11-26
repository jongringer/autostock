"""뉴스 가드 — 헤드라인 BLOCK 키워드 (fail-closed)."""

from __future__ import annotations

import logging
from typing import Iterable, Mapping

from ..config import NewsGuardConfig
from ..marketdata.news import match_keywords
from ..models import GuardDecision

LOG = logging.getLogger(__name__)


class NewsGuard:
    def __init__(self, provider, config: NewsGuardConfig) -> None:
        self.provider = provider
        self.config = config

    def decisions(self, symbols: Iterable[str]) -> Mapping[str, GuardDecision]:
        symbols = list(symbols)
        if not self.config.enabled:
            return {symbol: GuardDecision.ok(disabled=True) for symbol in symbols}
        out: dict[str, GuardDecision] = {}
        for symbol in symbols:
            try:
                titles = self.provider.headlines(symbol)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("뉴스 조회 실패 %s: %s", symbol, exc)
                titles = None
            if titles is None:
                if self.config.fail_closed:
                    out[symbol] = GuardDecision.block("news-unavailable", symbol=symbol)
                else:
                    out[symbol] = GuardDecision.ok()
                continue
            hits = match_keywords(titles[: self.config.lookback_items], self.config.block_keywords)
            if hits:
                out[symbol] = GuardDecision.block("news-block", symbol=symbol, hits=hits[:5])
            else:
                out[symbol] = GuardDecision.ok(headlines=len(titles))
        return out
