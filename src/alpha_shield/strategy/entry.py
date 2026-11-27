"""진입·물타기 판정 — ALPHA SHIELD v3.3 (Hark 1 · Hark 2).

- 신규 진입: 장중 고점 대비 `-0.52 x ATR(20)%` 이상 눌림 + `RSI(2) <= 15` + `주가 >= SMA200 x 0.95`
- 비정상 급락 가드: 장중 고점 대비 `-3.0 x ATR(20)%` 초과 폭락은 당일 신규 매수 제외
- 슬롯 점수: 알파 가중치 x min(|낙폭| / |진입 임계|, 2.5)
- 물타기: 평단 대비 max(1.5%, 0.8 x ATR(20)%) 이상 하락 + RSI(2) <= 35 + 1차 진입 후 2시간(또는 익거래일)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Sequence

from ..clock import to_et
from ..config import AddConfig, EntryConfig, Universe
from ..models import Position
from ..numeric import HUNDRED, ZERO, pct4


@dataclass(frozen=True)
class SymbolInputs:
    """한 종목의 판정 입력 스냅샷."""

    symbol: str
    last_price: Decimal
    hod: Decimal | None
    rsi2: Decimal | None
    atr_pct: Decimal | None
    sma200: Decimal | None
    quote_time: datetime | None = None


@dataclass(frozen=True)
class EntryEvaluation:
    symbol: str
    allowed: bool
    reason: str
    reasons: tuple[str, ...]
    drawdown_pct: Decimal
    threshold_pct: Decimal
    score: Decimal
    used_fallback: bool = False
    crash_pct: Decimal | None = None

    @property
    def abnormal_crash(self) -> bool:
        return "abnormal-crash" in self.reasons

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "allowed": self.allowed,
            "reason": self.reason,
            "reasons": list(self.reasons),
            "drawdown_pct": str(self.drawdown_pct),
            "threshold_pct": str(self.threshold_pct),
            "score": str(self.score),
            "used_fallback": self.used_fallback,
            "crash_pct": None if self.crash_pct is None else str(self.crash_pct),
        }


@dataclass(frozen=True)
class AddEvaluation:
    symbol: str
    allowed: bool
    reason: str
    gap_pct: Decimal
    required_pct: Decimal = ZERO

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "allowed": self.allowed,
            "reason": self.reason,
            "gap_pct": str(self.gap_pct),
            "required_pct": str(self.required_pct),
        }


def entry_threshold_pct(inputs: SymbolInputs, entry: EntryConfig, extra_pct: Decimal = ZERO) -> tuple[Decimal, bool]:
    """(요구 낙폭 %, ATR 폴백 사용 여부)."""
    if inputs.atr_pct is None or inputs.atr_pct <= ZERO:
        return entry.fallback_threshold_pct(inputs.symbol) + extra_pct, True
    return pct4(inputs.atr_pct * entry.atr_drawdown_multiple + extra_pct), False


def crash_threshold_pct(inputs: SymbolInputs, entry: EntryConfig) -> Decimal | None:
    """비정상 급락 가드 임계(%). ATR 결측이면 판정하지 않는다."""
    if inputs.atr_pct is None or inputs.atr_pct <= ZERO:
        return None
    return pct4(inputs.atr_pct * entry.crash_atr_multiple)


def drawdown_from_high(last_price: Decimal, hod: Decimal | None) -> Decimal:
    if hod is None or hod <= ZERO:
        return ZERO
    return pct4((last_price - hod) / hod * HUNDRED)


def slot_score(
    alpha: Decimal,
    drawdown_pct: Decimal,
    threshold_pct: Decimal,
    cap_multiple: Decimal,
) -> Decimal:
    """Hark 2 점수 — 낙폭 비율은 `cap_multiple` 배에서 상한 처리."""
    if threshold_pct <= ZERO:
        return ZERO
    ratio = abs(drawdown_pct) / threshold_pct
    if ratio > cap_multiple:
        ratio = cap_multiple
    return pct4(alpha * ratio)


def evaluate_entry(
    inputs: SymbolInputs,
    entry: EntryConfig,
    universe: Universe,
    *,
    extra_drawdown_pct: Decimal = ZERO,
) -> EntryEvaluation:
    """신규 진입(Hark 1) 판정.

    조건: 장중 고점 대비 -0.52xATR(20)% 이상 눌림 + RSI(2) <= 15 + 주가 >= SMA200 x 0.95
    차단: RSI(2) >= 70 추격, -3.0xATR(20)% 초과 급락, 데이터 결측(fail-closed)
    점수: 알파 가중치 x min(|낙폭| / |진입 임계|, 2.5)
    """
    threshold, used_fallback = entry_threshold_pct(inputs, entry, extra_drawdown_pct)
    drawdown = drawdown_from_high(inputs.last_price, inputs.hod)
    score = slot_score(
        universe.alpha(inputs.symbol), drawdown, threshold, entry.score_cap_multiple
    )
    crash_limit = crash_threshold_pct(inputs, entry)

    failures: list[str] = []
    if inputs.last_price <= ZERO:
        failures.append("invalid-price")
    if inputs.rsi2 is None:
        failures.append("rsi-unavailable")
    if inputs.sma200 is None:
        failures.append("sma200-unavailable")
    if inputs.hod is None:
        failures.append("hod-unavailable")
    if crash_limit is not None and drawdown <= -crash_limit:
        # 뉴스·공시 미탐지 돌발 악재로 간주해 당일 신규 매수에서 제외한다.
        failures.append("abnormal-crash")
    if inputs.sma200 is not None and inputs.last_price < inputs.sma200 * entry.sma_floor_ratio:
        failures.append("below-sma-floor")
    if inputs.rsi2 is not None and inputs.rsi2 >= entry.chase_rsi_block:
        failures.append("chase-block")
    if inputs.hod is not None and abs(drawdown) < threshold:
        failures.append("no-dip")
    if inputs.rsi2 is not None and inputs.rsi2 > entry.rsi_max:
        failures.append("rsi-too-high")
    return EntryEvaluation(
        symbol=inputs.symbol,
        allowed=not failures,
        reason=failures[0] if failures else "ok",
        reasons=tuple(failures),
        drawdown_pct=drawdown,
        threshold_pct=threshold,
        score=score,
        used_fallback=used_fallback,
        crash_pct=crash_limit,
    )


def add_required_pct(atr_pct: Decimal | None, add: AddConfig) -> Decimal:
    """물타기 요구 추가 하락 폭 = max(1.5%, 0.8 x ATR(20)%)."""
    required = add.trigger_pct
    if atr_pct is not None and atr_pct > ZERO:
        atr_based = pct4(atr_pct * add.atr_multiple)
        if atr_based > required:
            required = atr_based
    return required


def add_interval_elapsed(position: Position, add: AddConfig, now: datetime | None) -> bool:
    """1차 진입 후 최소 2시간(또는 익거래일)이 지났는가. 시각 미확인은 fail-closed."""
    if now is None:
        return False
    if to_et(now).date() > position.first_entry_date:
        return True
    return now - position.entry_at >= timedelta(hours=add.min_interval_hours)


def evaluate_add(
    position: Position,
    add: AddConfig,
    *,
    last_price: Decimal,
    rsi2: Decimal | None,
    atr_pct: Decimal | None = None,
    now: datetime | None = None,
) -> AddEvaluation:
    """물타기: max(1.5%, 0.8 x ATR(20)%) 이상 추가 하락 + RSI(2) <= 35 + 2시간 경과, 종목당 1회."""
    if position.avg_price <= ZERO:
        return AddEvaluation(position.symbol, False, "invalid-position", ZERO, add.trigger_pct)
    gap = pct4((last_price - position.avg_price) / position.avg_price * HUNDRED)
    required = add_required_pct(atr_pct, add)
    if position.add_count >= add.max_adds:
        return AddEvaluation(position.symbol, False, "max-adds", gap, required)
    if rsi2 is None:
        return AddEvaluation(position.symbol, False, "rsi-unavailable", gap, required)
    if gap > -required:
        return AddEvaluation(position.symbol, False, "no-add-dip", gap, required)
    if rsi2 > add.rsi_max:
        return AddEvaluation(position.symbol, False, "rsi-too-high", gap, required)
    if now is None:
        return AddEvaluation(position.symbol, False, "add-interval-unconfirmed", gap, required)
    if not add_interval_elapsed(position, add, now):
        return AddEvaluation(position.symbol, False, "add-interval", gap, required)
    return AddEvaluation(position.symbol, True, "ok", gap, required)


def evaluate_entries(
    inputs: Sequence[SymbolInputs],
    entry: EntryConfig,
    universe: Universe,
    *,
    extra_drawdown_pct: Decimal = ZERO,
) -> list[EntryEvaluation]:
    return [evaluate_entry(item, entry, universe, extra_drawdown_pct=extra_drawdown_pct) for item in inputs]
