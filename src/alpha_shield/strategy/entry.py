"""진입·물타기 판정 — ALPHA SHIELD v3.2 (Hark 1)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Sequence

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
        }


@dataclass(frozen=True)
class AddEvaluation:
    symbol: str
    allowed: bool
    reason: str
    gap_pct: Decimal

    def to_dict(self) -> dict[str, object]:
        return {"symbol": self.symbol, "allowed": self.allowed, "reason": self.reason, "gap_pct": str(self.gap_pct)}


def entry_threshold_pct(inputs: SymbolInputs, entry: EntryConfig, extra_pct: Decimal = ZERO) -> tuple[Decimal, bool]:
    """(요구 낙폭 %, ATR 폴백 사용 여부)."""
    if inputs.atr_pct is None or inputs.atr_pct <= ZERO:
        return entry.fallback_threshold_pct(inputs.symbol) + extra_pct, True
    return pct4(inputs.atr_pct * entry.atr_drawdown_multiple + extra_pct), False


def drawdown_from_high(last_price: Decimal, hod: Decimal | None) -> Decimal:
    if hod is None or hod <= ZERO:
        return ZERO
    return pct4((last_price - hod) / hod * HUNDRED)


def evaluate_entry(
    inputs: SymbolInputs,
    entry: EntryConfig,
    universe: Universe,
    *,
    extra_drawdown_pct: Decimal = ZERO,
) -> EntryEvaluation:
    """신규 진입(Hark 1) 판정.

    조건: 장중 고점 대비 -0.52xATR(20)% 이상 눌림 + RSI(2) <= 15 + 주가 >= SMA200 x 0.80
    차단: RSI(2) >= 70 추격, 데이터 결측(fail-closed)
    점수: 알파 가중치 x (|낙폭| / |진입 임계|)
    """
    threshold, used_fallback = entry_threshold_pct(inputs, entry, extra_drawdown_pct)
    drawdown = drawdown_from_high(inputs.last_price, inputs.hod)
    score = ZERO
    if threshold > ZERO:
        score = pct4(universe.alpha(inputs.symbol) * (abs(drawdown) / threshold))

    failures: list[str] = []
    if inputs.last_price <= ZERO:
        failures.append("invalid-price")
    if inputs.rsi2 is None:
        failures.append("rsi-unavailable")
    if inputs.sma200 is None:
        failures.append("sma200-unavailable")
    if inputs.hod is None:
        failures.append("hod-unavailable")
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
    )


def evaluate_add(
    position: Position,
    add: AddConfig,
    *,
    last_price: Decimal,
    rsi2: Decimal | None,
) -> AddEvaluation:
    """물타기: 평단 대비 -1.5% 이상 추가 하락 + RSI(2) <= 35, 종목당 1회."""
    gap = pct4((last_price - position.avg_price) / position.avg_price * HUNDRED)
    if position.add_count >= add.max_adds:
        return AddEvaluation(position.symbol, False, "max-adds", gap)
    if rsi2 is None:
        return AddEvaluation(position.symbol, False, "rsi-unavailable", gap)
    if gap > -add.trigger_pct:
        return AddEvaluation(position.symbol, False, "no-add-dip", gap)
    if rsi2 > add.rsi_max:
        return AddEvaluation(position.symbol, False, "rsi-too-high", gap)
    return AddEvaluation(position.symbol, True, "ok", gap)


def evaluate_entries(
    inputs: Sequence[SymbolInputs],
    entry: EntryConfig,
    universe: Universe,
    *,
    extra_drawdown_pct: Decimal = ZERO,
) -> list[EntryEvaluation]:
    return [evaluate_entry(item, entry, universe, extra_drawdown_pct=extra_drawdown_pct) for item in inputs]
