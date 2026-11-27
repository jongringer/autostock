"""청산 판정 — 손절 / VIX 가변 목표 익절 / 시간청산 / 트레일링 전환.

v3.3:
- 시간청산 만기 시 +3.0% 이상 상승 추세 종목은 고점 대비 -2.0% 트레일링으로 전환(옵션).
- 14:50~14:58 ET 선제 청산은 엔진이 세션 창과 함께 판단한다(여기서는 순수 규칙만).
"""

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
    trail_high: Decimal | None = None
    arm_trailing: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "should_exit": self.should_exit,
            "reason": self.reason,
            "pct": str(self.pct),
            "trigger_price": None if self.trigger_price is None else str(self.trigger_price),
            "trail_high": None if self.trail_high is None else str(self.trail_high),
            "arm_trailing": self.arm_trailing,
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


def preemptive_near_stop_pct(position: Position, cfg: ExitConfig) -> Decimal:
    """14:50~14:58 ET 선제 청산 대상이 되는 근접 손절선(%)."""
    stop = stop_loss_pct(position, cfg)
    near = stop - cfg.preemptive_near_stop_pct
    return near if near > Decimal("0") else stop


def trailing_exit_price(trail_high: Decimal, cfg: ExitConfig) -> Decimal:
    return trail_high * (Decimal(1) - cfg.trailing_drawdown_pct / HUNDRED)


def evaluate_exit(
    position: Position,
    last_price: Decimal,
    cfg: ExitConfig,
    *,
    trading_days_held: int,
    calendar_days_held: int,
    trail_high: Decimal | None = None,
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

    if trail_high is not None and trail_high > 0:
        trigger = trailing_exit_price(trail_high, cfg)
        if last_price <= trigger:
            return ExitEvaluation(
                position.symbol, True, "trailing_stop", pct, trigger, trail_high=trail_high
            )
        # 트레일링 운영 중에는 시간청산 만기를 다시 적용하지 않는다(전환 뒤에는 트레일링이 청산 규칙).
        return ExitEvaluation(position.symbol, False, "trailing_hold", pct, trail_high=trail_high)

    if trading_days_held >= cfg.time_exit_trading_days or calendar_days_held >= cfg.time_exit_calendar_days:
        if cfg.trailing_on_time_exit and pct >= cfg.trailing_trigger_pct:
            # 만기지만 아직 상승 추세 — 바로 팔지 않고 고점 대비 -2.0% 트레일링으로 넘긴다.
            return ExitEvaluation(
                position.symbol,
                False,
                "trailing_armed",
                pct,
                trail_high=trail_high,
                arm_trailing=True,
            )
        return ExitEvaluation(position.symbol, True, "time_exit", pct, None, trail_high=trail_high)

    return ExitEvaluation(position.symbol, False, "hold", pct, trail_high=trail_high)


def hold_days(position: Position, today, calendar) -> tuple[int, int]:
    """(보유 거래일 수, 보유 달력일 수). 진입일 당일은 0일."""
    calendar_days = (today - position.first_entry_date).days
    trading_days = calendar.trading_days_between(position.first_entry_date, today)
    return max(trading_days, 0), max(calendar_days, 0)
