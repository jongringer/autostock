"""거시 가드 — FOMC/CPI/NFP 당일 요구 낙폭 -0.5%p 강화."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from ..config import MacroGuardConfig
from ..models import GuardDecision
from ..numeric import ZERO


class MacroGuard:
    def __init__(self, calendar, config: MacroGuardConfig) -> None:
        self.calendar = calendar  # None 이면 파일 로딩 실패
        self.config = config

    def decision(self) -> GuardDecision:
        if self.calendar is None and self.config.fail_closed:
            return GuardDecision.block("macro-calendar-unavailable")
        return GuardDecision.ok()

    def drawdown_boost_pct(self, day: date) -> Decimal:
        if self.calendar is None:
            return ZERO
        return self.calendar.drawdown_boost_pct(day)

    def events_on(self, day: date) -> tuple[str, ...]:
        if self.calendar is None:
            return ()
        return self.calendar.events_on(day)
