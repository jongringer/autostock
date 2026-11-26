"""청산 판정 — 손절 / VIX 가변 목표 익절 / 시간청산."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..config import ExitConfig
from ..models import Position
from ..numeric import HUNDRED, pct4


@dataclass(frozen=True)
class ExitEvaluation:
    symbol: str
    should_exit: bool
    reason: str
    pct: Decimal  # 현재 평가 손익률(%)
    trigger_price: Decimal | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "should_exit": self.should_exit,
            "reason": self.reason,
            "pct": str(self.pct),
            "trigger_price": None if self.trigger_price is None else str(self.trigger_price),
        }


def vix_take_profit_pct(vix_close: Decimal | None, cfg: ExitConfig) -> Decimal:
    """진입 시점 VIX 종가로 확정하는 목표 익절률.

    VIX < 18.0 -> +8.0%,  VIX >= 18.0 또는 미확인 -> +5.0%
    """
    if vix_close is not None and vix_close < cfg.take_profit_vix_low:
        return cfg.take_profit_low_vix_pct
    return cfg.take_profit_high_vix_pct


def stop_loss_pct(position: Position, cfg: ExitConfig) -> Decimal:
    """단일 -5.0% / 물타기 포지션 -3.5%."""
    return cfg.stop_loss_added_pct if position.add_count >= 1 else cfg.stop_loss_pct


def evaluate_exit(
    position: Position,
    last_price: Decimal,
    cfg: ExitConfig,
    *,
    trading_days_held: int,
    calendar_days_held: int,
) -> ExitEvaluation:
    if position.avg_price <= 0:
        return ExitEvaluation(position.symbol, False, "invalid-position", Decimal("0"))
    pct = pct4((last_price - position.avg_price) / position.avg_price * HUNDRED)

    stop_pct = stop_loss_pct(position, cfg)
    if pct <= -stop_pct:
        return ExitEvaluation(
            position.symbol, True, "stop_loss", pct, position.avg_price * (Decimal(1) - stop_pct / HUNDRED)
        )

    target = position.tp_target_pct
    if pct >= target:
        return ExitEvaluation(
            position.symbol, True, "take_profit", pct, position.avg_price * (Decimal(1) + target / HUNDRED)
        )

    if trading_days_held >= cfg.time_exit_trading_days or calendar_days_held >= cfg.time_exit_calendar_days:
        return ExitEvaluation(position.symbol, True, "time_exit", pct, None)

    return ExitEvaluation(position.symbol, False, "hold", pct)


def hold_days(position: Position, today, calendar) -> tuple[int, int]:
    """(보유 거래일 수, 보유 달력일 수). 진입일 당일은 0일."""
    calendar_days = (today - position.first_entry_date).days
    trading_days = calendar.trading_days_between(position.first_entry_date, today)
    return max(trading_days, 0), max(calendar_days, 0)
