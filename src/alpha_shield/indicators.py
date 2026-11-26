"""기술적 지표 — Wilder 스무딩, 전 구간 Decimal.

스펙 기준값:
- ATR(20): Wilder, 초기 20개 TR 평균 후 `(prev*(n-1)+TR)/n`
- RSI(2): Wilder
- SMA(200): 단순이동평균
"""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from .models import Bar
from .numeric import ZERO, dec, pct4, safe_div


def sma(values: Sequence[Decimal], period: int) -> Decimal | None:
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period:
        return None
    window = values[-period:]
    return sum(window, ZERO) / Decimal(period)


def sma_series(values: Sequence[Decimal], period: int) -> list[Decimal | None]:
    out: list[Decimal | None] = []
    running = ZERO
    for index, value in enumerate(values):
        running += value
        if index >= period:
            running -= values[index - period]
        out.append(running / Decimal(period) if index >= period - 1 else None)
    return out


def true_ranges(bars: Sequence[Bar]) -> list[Decimal]:
    """TR = max(high-low, |high-prev_close|, |low-prev_close|). 첫 봉은 high-low."""
    if not bars:
        return []
    ranges: list[Decimal] = []
    previous_close = bars[0].close
    for index, bar in enumerate(bars):
        if index == 0:
            ranges.append(bar.high - bar.low)
        else:
            ranges.append(max(bar.high - bar.low, abs(bar.high - previous_close), abs(bar.low - previous_close)))
        previous_close = bar.close
    return ranges


def wilder_atr(bars: Sequence[Bar], period: int = 20) -> Decimal | None:
    """Wilder ATR. bars 는 오래된 -> 최신 순."""
    if period <= 0:
        raise ValueError("period must be positive")
    if len(bars) < period:
        return None
    ranges = true_ranges(bars)
    atr = sum(ranges[:period], ZERO) / Decimal(period)
    for value in ranges[period:]:
        atr = (atr * Decimal(period - 1) + value) / Decimal(period)
    return atr


def atr_pct(bars: Sequence[Bar], period: int = 20) -> Decimal | None:
    """ATR / 최신 종가 x 100."""
    atr = wilder_atr(bars, period)
    if atr is None or not bars:
        return None
    close = bars[-1].close
    if close <= ZERO:
        return None
    return pct4(atr / close * Decimal(100))


def wilder_rsi(closes: Sequence[Decimal], period: int = 2) -> Decimal | None:
    """Wilder RSI. closes 는 오래된 -> 최신 순. period+1 개 이상 필요."""
    if period <= 0:
        raise ValueError("period must be positive")
    if len(closes) < period + 1:
        return None
    gains = ZERO
    losses = ZERO
    for index in range(1, period + 1):
        change = closes[index] - closes[index - 1]
        if change > ZERO:
            gains += change
        else:
            losses += -change
    avg_gain = gains / Decimal(period)
    avg_loss = losses / Decimal(period)
    for index in range(period + 1, len(closes)):
        change = closes[index] - closes[index - 1]
        gain = change if change > ZERO else ZERO
        loss = -change if change < ZERO else ZERO
        avg_gain = (avg_gain * Decimal(period - 1) + gain) / Decimal(period)
        avg_loss = (avg_loss * Decimal(period - 1) + loss) / Decimal(period)
    if avg_loss == ZERO:
        return Decimal("100") if avg_gain > ZERO else Decimal("50")
    rs = safe_div(avg_gain, avg_loss)
    if rs is None:  # pragma: no cover - avg_loss != 0 이므로 도달하지 않음
        return None
    return pct4(Decimal(100) - (Decimal(100) / (Decimal(1) + rs)))


def robust_intraday_high(
    bars: Sequence[Bar],
    *,
    spike_ratio: Decimal = Decimal("1.15"),
    enabled: bool = True,
) -> Decimal | None:
    """장중 고점(HOD) — 1분봉 이상 체결 필터.

    토스 미국 1분봉은 장외·특수 체결을 걸러내지 않는다. 한 봉의 high 가
    같은 세션 봉들의 high 중앙값의 `spike_ratio` 배를 넘으면 이상치로 보고
    그 봉은 max(open, close) 로 대체한다. 필터 후 HOD 를 돌려준다.
    """
    if not bars:
        return None
    highs = sorted(bar.high for bar in bars)
    median = highs[len(highs) // 2]
    limit = median * spike_ratio if median > ZERO else None
    hod = ZERO
    for bar in bars:
        high = bar.high
        if enabled and limit is not None and high > limit:
            high = max(bar.open, bar.close)
        if high > hod:
            hod = high
    return hod if hod > ZERO else None


def bollinger_band_width(closes: Sequence[Decimal], period: int = 20) -> Decimal | None:
    """(선택 지표) 볼린저 밴드 폭 — 진단 로그용."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    mean = sum(window, ZERO) / Decimal(period)
    if mean == ZERO:
        return None
    variance = sum(((value - mean) ** 2 for value in window), ZERO) / Decimal(period)
    std = variance.sqrt()
    return pct4((std * Decimal(4)) / mean * Decimal(100))


def decimal_series(values: Sequence[object]) -> list[Decimal]:
    return [dec(value) for value in values]
