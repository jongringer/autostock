"""이벤트 가드 — 실적 D-1 ~ 당일 신규 매수 차단 (Nasdaq 캘린더)."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Iterable, Mapping

from ..config import EventsGuardConfig
from ..models import GuardDecision

LOG = logging.getLogger(__name__)


class EarningsGuard:
    def __init__(self, provider, config: EventsGuardConfig, calendar=None) -> None:
        self.provider = provider
        self.config = config
        self.calendar = calendar

    def next_trading_day(self, day: date) -> date:
        cursor = day + timedelta(days=1)
        for _ in range(10):
            if self.calendar is None or self.calendar.is_trading_day(cursor):
                return cursor
            cursor += timedelta(days=1)
        return cursor

    def decisions(self, symbols: Iterable[str], today: date) -> Mapping[str, GuardDecision]:
        symbols = list(symbols)
        today_set = self.provider.symbols_with_earnings(today)
        tomorrow_set = self.provider.symbols_with_earnings(self.next_trading_day(today))
        if today_set is None or tomorrow_set is None:
            if self.config.earnings_fail_closed:
                return {symbol: GuardDecision.block("earnings-calendar-unavailable") for symbol in symbols}
            return {symbol: GuardDecision.ok() for symbol in symbols}
        out: dict[str, GuardDecision] = {}
        for symbol in symbols:
            if symbol in today_set:
                out[symbol] = GuardDecision.block("earnings-today", earnings_date=today.isoformat())
            elif symbol in tomorrow_set:
                out[symbol] = GuardDecision.block(
                    "earnings-tomorrow", earnings_date=self.next_trading_day(today).isoformat()
                )
            else:
                out[symbol] = GuardDecision.ok()
        return out
