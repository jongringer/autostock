"""데이터 안전장치 — 시세 15분 지연·결측, SMA200 미확인이면 매수 보류 (fail-closed).

매도는 이 가드에 걸리지 않는다 (fail-open) — 엔진은 보유 포지션 청산을 항상 시도한다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from ..config import DataGuardConfig
from ..models import Bar, GuardDecision, Quote


class DataGuard:
    def __init__(self, config: DataGuardConfig) -> None:
        self.config = config

    def check(
        self,
        symbol: str,
        quote: Quote | None,
        daily_bars: Sequence[Bar],
        now: datetime,
        *,
        sma_period: int = 200,
    ) -> GuardDecision:
        if quote is None:
            return GuardDecision.block("quote-missing", symbol=symbol)
        age = quote.age_seconds(now)
        if quote.timestamp is None or age is None:
            return GuardDecision.block("quote-timestamp-missing", symbol=symbol)
        if age > self.config.max_quote_age_seconds:
            return GuardDecision.block(
                "quote-stale", symbol=symbol, age_seconds=age, max_age=self.config.max_quote_age_seconds
            )
        if self.config.require_sma200:
            if len(daily_bars) < sma_period:
                return GuardDecision.block(
                    "sma200-unavailable", symbol=symbol, bars=len(daily_bars), required=sma_period
                )
            last_bar = daily_bars[-1]
            bar_age_days = (now.date() - last_bar.timestamp.date()).days
            if bar_age_days > self.config.max_daily_bar_age_days:
                return GuardDecision.block("daily-bars-stale", symbol=symbol, age_days=bar_age_days)
        return GuardDecision.ok(symbol=symbol, quote_age_seconds=age)
