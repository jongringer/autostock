"""이중 안전 잠금과 주문 폭주·일일 손실 방어.

실주문은 아래 두 조건이 **모두** 참일 때만 허용된다.
  1) 환경변수 `LIVE_ORDERS=true`
  2) `state/ARMED` 파일 존재
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from .config import RiskConfig
from .models import GuardDecision
from .numeric import HUNDRED, ZERO
from .state.repo import Repo


class LiveOrdersDisabled(RuntimeError):
    """이중 잠금이 풀리지 않은 상태에서 실주문을 시도했다."""


class LiveOrdersEnabledButNotArmed(LiveOrdersDisabled):
    pass


class SafetyLock:
    def __init__(self, env_enabled: bool, armed_file: Path | str) -> None:
        self.env_enabled = bool(env_enabled)
        self.armed_file = Path(armed_file)

    @property
    def armed(self) -> bool:
        return self.armed_file.exists()

    @property
    def live_allowed(self) -> bool:
        return self.env_enabled and self.armed

    def assert_live(self) -> None:
        if not self.env_enabled:
            raise LiveOrdersDisabled(
                "LIVE_ORDERS=true 가 아닙니다. 실주문은 환경변수 LIVE_ORDERS=true 와 "
                f"{self.armed_file} 파일이 모두 있어야 합니다."
            )
        if not self.armed:
            raise LiveOrdersEnabledButNotArmed(
                f"{self.armed_file} 파일이 없습니다. 실주문하려면 `touch {self.armed_file}` 후 다시 시작하세요."
            )

    def describe(self) -> dict[str, bool]:
        return {"live_orders_env": self.env_enabled, "armed_file": self.armed, "live_allowed": self.live_allowed}


class HaltSwitch:
    """킬스위치 — `state/halt.flag` 가 있으면 신규 매수 정지(청산은 허용)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    @property
    def halted(self) -> bool:
        return self.path.exists()

    def reason(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8").strip() or "halt.flag"
        except OSError:
            return "halt.flag"


class OrderRateGuard:
    """어뷰징 방지 — 1분 내 주문 6건 초과, 동일 종목·방향 60초 내 중복 차단."""

    def __init__(self, repo: Repo, config: RiskConfig) -> None:
        self.repo = repo
        self.config = config

    def check(self, symbol: str, side: str, now: datetime) -> GuardDecision:
        minute_ago = now - timedelta(seconds=60)
        count = self.repo.count_orders(minute_ago)
        if count >= self.config.max_orders_per_minute:
            return GuardDecision.block("order-rate-limit", orders_last_minute=count)
        window_start = now - timedelta(seconds=self.config.duplicate_order_window_seconds)
        for record in self.repo.orders_since(window_start):
            if record.symbol == symbol and record.side == side and record.status not in ("REJECTED", "CANCELED"):
                return GuardDecision.block("duplicate-order", existing=record.client_order_id)
        return GuardDecision.ok(orders_last_minute=count)


class DailyRiskGuard:
    """당일 자산 -5% 도달 시 그날 신규 매수 정지."""

    def __init__(self, repo: Repo, config: RiskConfig) -> None:
        self.repo = repo
        self.config = config

    def check(self, trade_date, equity, now: datetime) -> GuardDecision:
        state = self.repo.ensure_day_state(trade_date, equity, now)
        if state.buys_halted_reason:
            return GuardDecision.block(state.buys_halted_reason)
        if state.equity_open <= ZERO:
            return GuardDecision.ok()
        drawdown = (equity - state.equity_open) / state.equity_open * HUNDRED
        if drawdown <= -self.config.daily_loss_limit_pct:
            self.repo.halt_buys(trade_date, "daily-loss-limit", now)
            return GuardDecision.block(
                "daily-loss-limit",
                equity_open=str(state.equity_open),
                equity_now=str(equity),
                drawdown_pct=str(drawdown),
            )
        return GuardDecision.ok(drawdown_pct=str(drawdown))
