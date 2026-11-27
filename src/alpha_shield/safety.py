"""이중 안전 잠금 · 주문 폭주·일일 손실 방어 · 계좌 단위 Kill Switch(Safe Halt).

실주문은 아래 두 조건이 **모두** 참일 때만 허용된다.
  1) 환경변수 `LIVE_ORDERS=true`
  2) `state/ARMED` 파일 존재

계좌 Kill Switch(v3.3)는 다음 중 하나로 발동하며, 발동 시 신규 주문(매수·매도)을 전면 정지한다.
  - 일일 손실(실현+미실현) -4.0% 도달
  - 누적 최고 자산(MDD) 대비 -12.0% 돌파
  - 5분 내 동일 종목·방향 중복 주문 시도
  - API 3회 연속 오류
  - 장부·실잔고 불일치 감지
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Mapping, Sequence

from .config import KillSwitchConfig, RiskConfig
from .models import GuardDecision
from .numeric import HUNDRED, ZERO, dec
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
    """수동 킬스위치 — `state/halt.flag` 가 있으면 신규 매수 정지(청산은 허용)."""

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
    """당일 자산 -4% 도달 시 그날 신규 매수 정지 (Kill Switch 이전의 1차 방벽)."""

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


KV_KILL_SWITCH = "kill_switch"
KV_EQUITY_PEAK = "equity_peak"
KV_MDD = "mdd_state"
KV_API_ERRORS = "api_error_streak"


class AccountKillSwitch:
    """계좌 단위 Kill Switch — 발동하면 신규 주문을 전면 정지한다(Safe Halt)."""

    def __init__(self, repo: Repo, config: KillSwitchConfig) -> None:
        self.repo = repo
        self.config = config

    # ------------------------------------------------------------------ 상태
    def state(self) -> dict[str, object]:
        value = self.repo.kv_get_json(KV_KILL_SWITCH)
        return value if isinstance(value, dict) else {"active": False}

    @property
    def active(self) -> bool:
        return bool(self.state().get("active"))

    def reason(self) -> str:
        return str(self.state().get("reason") or "")

    def detail(self) -> Mapping[str, object]:
        value = self.state().get("detail")
        return value if isinstance(value, Mapping) else {}

    def tripped_at(self) -> str | None:
        value = self.state().get("at")
        return None if value is None else str(value)

    def trip(self, reason: str, now: datetime, detail: Mapping[str, object] | None = None) -> bool:
        """발동. 이미 발동 중이면 False (알림 중복 방지)."""
        if self.active:
            return False
        self.repo.kv_set_json(
            KV_KILL_SWITCH,
            {
                "active": True,
                "reason": reason,
                "at": now.isoformat(),
                "detail": dict(detail or {}),
            },
        )
        return True

    def reset(self, now: datetime) -> None:
        self.repo.kv_set_json(
            KV_KILL_SWITCH, {"active": False, "reason": "", "at": None, "reset_at": now.isoformat()}
        )

    # ------------------------------------------------------------------ 점검
    def check_daily_loss(self, equity, equity_open, now: datetime) -> bool:
        if equity_open is None or equity_open <= ZERO:
            return False
        pct = (equity - equity_open) / equity_open * HUNDRED
        if pct <= -self.config.daily_loss_pct:
            return self.trip(
                "daily-loss",
                now,
                {"equity_open": str(equity_open), "equity": str(equity), "pct": str(pct)},
            )
        return False

    def observe_equity(self, equity, now: datetime) -> bool:
        """최고 자산을 갱신하고 누적 MDD 를 점검한다."""
        raw_peak = self.repo.kv_get(KV_EQUITY_PEAK)
        peak = dec(raw_peak) if raw_peak else equity
        if equity > peak:
            peak = equity
            self.repo.kv_set(KV_EQUITY_PEAK, str(peak))
        mdd = ((equity - peak) / peak * HUNDRED) if peak > ZERO else ZERO
        self.repo.kv_set_json(
            KV_MDD, {"peak": str(peak), "equity": str(equity), "mdd_pct": str(mdd), "at": now.isoformat()}
        )
        if mdd <= -self.config.mdd_pct:
            return self.trip("max-drawdown", now, {"peak": str(peak), "equity": str(equity), "mdd_pct": str(mdd)})
        return False

    def mdd_pct(self) -> str | None:
        value = self.repo.kv_get_json(KV_MDD)
        return None if not isinstance(value, dict) else str(value.get("mdd_pct"))

    def peak_equity(self) -> str | None:
        value = self.repo.kv_get_json(KV_MDD)
        return None if not isinstance(value, dict) else str(value.get("peak"))

    # ------------------------------------------------------------------ API 오류
    def api_error_streak(self) -> int:
        raw = self.repo.kv_get(KV_API_ERRORS, "0")
        try:
            return int(raw or "0")
        except ValueError:
            return 0

    def note_api_error(self, now: datetime) -> bool:
        streak = self.api_error_streak() + 1
        self.repo.kv_set(KV_API_ERRORS, str(streak))
        if streak >= self.config.api_error_threshold:
            return self.trip("api-errors", now, {"streak": streak})
        return False

    def note_api_success(self) -> None:
        if self.api_error_streak():
            self.repo.kv_set(KV_API_ERRORS, "0")

    # ------------------------------------------------------------------ 중복·불일치
    def note_order_attempt(self, symbol: str, side: str, now: datetime) -> GuardDecision:
        """5분 내 동일 종목·방향 주문 시도 → Safe Halt."""
        window_start = now - timedelta(seconds=self.config.duplicate_window_seconds)
        for record in self.repo.orders_since(window_start):
            if record.symbol != symbol or record.side != side:
                continue
            if record.status in ("REJECTED", "CANCELED"):
                continue
            if record.created_at > window_start:
                tripped = self.trip(
                    "duplicate-order",
                    now,
                    {"symbol": symbol, "side": side, "existing": record.client_order_id},
                )
                if tripped:
                    return GuardDecision.block("kill-switch:duplicate-order", existing=record.client_order_id)
        return GuardDecision.ok()

    def check_mismatch(self, mismatches: Sequence[str] | None, now: datetime) -> bool:
        if not self.config.halt_on_mismatch:
            return False
        if mismatches:
            return self.trip("book-mismatch", now, {"mismatches": list(mismatches)[:6]})
        return False

    def describe(self) -> dict[str, object]:
        state = self.state()
        return {
            "active": bool(state.get("active")),
            "reason": state.get("reason") or "",
            "at": state.get("at"),
            "detail": state.get("detail") or {},
            "api_error_streak": self.api_error_streak(),
            "peak_equity": self.peak_equity(),
            "mdd_pct": self.mdd_pct(),
        }
