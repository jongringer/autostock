"""데이터 안전장치 — 시세 지연·결측·SMA200 미확인이면 매수 보류(fail-closed),
매도 쪽은 이상 틱 가드(직전 5분봉 대비 ±10% 급변 시 보조 시세 크로스체크)를 통과해야 한다.

매도는 이 가드에 걸리지 않는다 (fail-open) — 단, 이상 틱으로 보이는 가격은
보조 소스가 확인해 줄 때만 손절로 집행한다(가짜 손절 방지).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable, Sequence

from ..config import DataGuardConfig
from ..models import Bar, GuardDecision, Quote
from ..numeric import HUNDRED, ZERO, pct4


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


def reference_close(
    bars: Sequence[Bar],
    now: datetime,
    minutes: int = 5,
) -> Decimal | None:
    """직전 5분봉 기준 종가 — `now - minutes` 이전에 끝난 마지막 1분봉의 종가.

    bars 는 오래된 -> 최신 순, timestamp 는 봉 종료 시각.
    """
    if not bars:
        return None
    cutoff = now - timedelta(minutes=minutes)
    candidates = [bar for bar in bars if bar.timestamp <= cutoff and bar.close > ZERO]
    if not candidates:
        return None
    latest = candidates[-1]
    # '직전 5분봉'은 지금과 가까운 봉이어야 한다. 오래된 봉과 비교해 급변으로 오판하지 않는다.
    if now - latest.timestamp > timedelta(minutes=minutes * 2):
        return None
    return latest.close


class TickGuard:
    """이상 틱 가드 — 직전 5분봉 대비 ±10% 이상 급변하면 보조 시세로 교차 확인한다."""

    def __init__(self, config: DataGuardConfig) -> None:
        self.config = config

    def check(
        self,
        symbol: str,
        price: Decimal,
        bars: Sequence[Bar],
        now: datetime,
        crosscheck: Callable[[str], Decimal | None] | None = None,
    ) -> GuardDecision:
        reference = reference_close(bars, now, self.config.tick_guard_bar_minutes)
        if reference is None or reference <= ZERO or price <= ZERO:
            return GuardDecision.ok(symbol=symbol, reference=None, spike=False)
        change = pct4((price - reference) / reference * HUNDRED)
        if abs(change) < self.config.tick_guard_pct:
            return GuardDecision.ok(symbol=symbol, reference=str(reference), change_pct=str(change), spike=False)

        secondary: Decimal | None = None
        if crosscheck is not None:
            try:
                secondary = crosscheck(symbol)
            except Exception:  # noqa: BLE001 - 보조 소스 실패는 미확인으로 처리
                secondary = None
        if secondary is None or secondary <= ZERO:
            if self.config.tick_guard_fail_closed:
                return GuardDecision.block(
                    "tick-guard-crosscheck-unavailable",
                    symbol=symbol,
                    change_pct=str(change),
                    reference=str(reference),
                )
            return GuardDecision.ok(symbol=symbol, change_pct=str(change), spike=True, verified=False)

        diff_pct = pct4(abs(secondary - price) / price * HUNDRED)
        if diff_pct > self.config.tick_guard_tolerance_pct:
            return GuardDecision.block(
                "tick-guard-crosscheck-mismatch",
                symbol=symbol,
                change_pct=str(change),
                reference=str(reference),
                secondary=str(secondary),
                diff_pct=str(diff_pct),
            )
        return GuardDecision.ok(
            symbol=symbol,
            change_pct=str(change),
            reference=str(reference),
            secondary=str(secondary),
            spike=True,
            verified=True,
        )


def is_spike(decision: GuardDecision) -> bool:
    return bool(decision.detail.get("spike"))


__all__ = ["DataGuard", "TickGuard", "reference_close", "is_spike"]
