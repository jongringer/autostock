"""매매 엔진 — 5분 사이클.

판단 우선순위(v3.3): **(1) 계좌 Kill Switch 점검 → (2) 매도 → (3) 매수(신규·물타기)**.
  - 매도: 프리마켓 예약분 집행 → 손절(오버나잇은 09:32 ET 이후) · 익절 · 시간청산/트레일링
          · 14:50~14:58 ET 선제 청산
  - 매수: 시장·세션·이벤트·공시·뉴스·거시·데이터·계좌규정 가드 → Hark 2 점수 순위 → 빈 슬롯

모든 판정 근거는 로그·JSONL·DB 에 남긴다. 매수는 fail-closed, 매도는 fail-open.
단, 계좌 Kill Switch(Safe Halt)가 발동하면 신규 주문은 매도까지 전면 정지한다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ..calendar import MarketCalendar, SessionEvaluation, SessionGuard
from ..clock import Clock, SystemClock, et_date
from ..config import GuardsConfig, StrategyConfig
from ..guards.account import AccountRegulationGuard
from ..guards.data import DataGuard, TickGuard
from ..guards.events import EarningsGuard
from ..guards.filings import FilingsGuard
from ..guards.macro import MacroGuard
from ..guards.market import MarketGuard
from ..guards.news import NewsGuard
from ..indicators import atr_pct, robust_intraday_high, sma, wilder_rsi
from ..models import (
    ORDER_STATUS_DRY_RUN,
    ORDER_STATUS_FILLED,
    ORDER_STATUS_PARTIAL,
    ORDER_STATUS_REJECTED,
    AccountState,
    Bar,
    CycleReport,
    GuardDecision,
    OrderRequest,
    OrderResult,
    Position,
    Quote,
)
from ..numeric import ZERO, dec, qty6
from ..safety import AccountKillSwitch, DailyRiskGuard, HaltSwitch, LiveOrdersDisabled, OrderRateGuard
from ..state.repo import Repo
from ..strategy.cooldown import REASON_STOP_LOSS_EOD, CooldownBook
from ..strategy.entry import EntryEvaluation, SymbolInputs, evaluate_add, evaluate_entry
from ..strategy.exit import (
    evaluate_exit,
    hold_days,
    preemptive_near_stop_pct,
    vix_take_profit_pct,
)
from ..strategy.signals import SignalBook
from ..strategy.sizing import Budget, plan_add, plan_new_entry, spendable_cash
from ..strategy.slots import free_slots, rank_candidates
from .alerts import Alerter, DecisionLog
from .reconcile import reconcile

LOG = logging.getLogger(__name__)

PURPOSE_BY_REASON = {
    "stop_loss": "s",
    "take_profit": "t",
    "time_exit": "x",
    "time_exit_no_quote": "x",
    "trailing_stop": "s",
    "eod_preemptive_stop": "s",
    "eod_preemptive_time_exit": "x",
}

STOP_FAMILY = ("stop_loss", "eod_preemptive_stop")
MIN_QTY = Decimal("0.000001")
KV_PREMARKET_QUEUE = "premarket_sell_queue"
KV_CRASH_GUARD = "crash_guard"


@dataclass(frozen=True)
class CycleContext:
    now: datetime
    today: date
    session: SessionEvaluation
    quotes: Mapping[str, Quote]
    account: AccountState
    daily: Mapping[str, Sequence[Bar]]
    intraday: Mapping[str, Sequence[Bar]]


class Engine:
    def __init__(
        self,
        *,
        mode: str,
        strategy: StrategyConfig,
        guards_config: GuardsConfig,
        repo: Repo,
        broker,
        market_data,
        calendar: MarketCalendar,
        clock: Clock | None = None,
        halt_file: Path | None = None,
        alerter: Alerter | None = None,
        decision_log: DecisionLog | None = None,
        earnings_provider=None,
        filings_provider=None,
        news_provider=None,
        macro_calendar=None,
        allow_fractional_premarket_sell: bool = False,
        tick_crosscheck: Callable[[str], Decimal | None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.mode = mode
        self.strategy = strategy
        self.guards_config = guards_config
        self.repo = repo
        self.broker = broker
        self.market_data = market_data
        self.calendar = calendar
        self.clock = clock or SystemClock()
        self.alerter = alerter or Alerter(clock=self.clock.now)
        self.decision_log = decision_log or DecisionLog(None)
        self._sleep = sleep
        self.allow_fractional_premarket_sell = bool(allow_fractional_premarket_sell)
        self.tick_crosscheck = tick_crosscheck

        self.session_guard = SessionGuard(calendar, guards_config)
        self.market_guard = MarketGuard(market_data, guards_config.market)
        self.data_guard = DataGuard(guards_config.data)
        self.tick_guard = TickGuard(guards_config.data)
        self.macro_guard = MacroGuard(macro_calendar, guards_config.macro)
        self.account_guard = AccountRegulationGuard(repo, guards_config.account, calendar)
        self.earnings_guard = (
            EarningsGuard(earnings_provider, guards_config.events, calendar) if earnings_provider is not None else None
        )
        self.filings_guard = (
            FilingsGuard(filings_provider, guards_config.filings) if filings_provider is not None else None
        )
        self.news_guard = NewsGuard(news_provider, guards_config.news) if news_provider is not None else None
        self.signals = SignalBook(repo, strategy.entry.signal_redelivery_rsi_max)
        self.cooldowns = CooldownBook(repo, strategy.exit.cooldown_seconds)
        self.rate_guard = OrderRateGuard(repo, guards_config.risk)
        self.risk_guard = DailyRiskGuard(repo, guards_config.risk)
        self.kill_switch = AccountKillSwitch(repo, guards_config.kill_switch)
        self.halt = HaltSwitch(halt_file) if halt_file is not None else None
        self._vix_value: Decimal | None = None
        self._vix_date: date | None = None

    # ================================================================== 공개
    def bootstrap(self) -> None:
        """기동 시 대조(reconcile). 불일치는 신규 매수 중단 사유로 남는다."""
        now = self.clock.now()
        today = et_date(now)
        try:
            account = self.broker.account()
            self.kill_switch.note_api_success()
        except Exception as exc:  # noqa: BLE001
            self.alerter.notify("error", "계좌 조회 실패 — 매수 중단", error=str(exc))
            self._note_api_failure(now, "bootstrap-account", exc)
            self.repo.kv_set_json(
                "reconcile_state",
                {"date": today.isoformat(), "ok": False, "mismatches": [f"account-error: {exc}"]},
            )
            return
        # 기동 시에는 같은 날짜에 이미 대조했더라도 다시 본다 (재시작 후 잔고가 달라졌을 수 있음)
        self._reconcile(account, today, now, force=True)

    def run_forever(self, interval_seconds: int = 300, max_cycles: int | None = None) -> None:
        completed = 0
        LOG.info("엔진 시작 (mode=%s, interval=%ss)", self.mode, interval_seconds)
        while max_cycles is None or completed < max_cycles:
            try:
                self.run_cycle()
            except LiveOrdersDisabled:
                raise
            except Exception as exc:  # noqa: BLE001 - 한 사이클 실패가 봇을 죽이지 않게
                LOG.exception("사이클 실패: %s", exc)
                self.alerter.notify("error", "사이클 예외", error=str(exc))
            completed += 1
            if max_cycles is not None and completed >= max_cycles:
                break
            self._sleep(interval_seconds)

    # ================================================================== 사이클
    def run_cycle(self) -> CycleReport:
        now = self.clock.now()
        today = et_date(now)
        session = self.session_guard.evaluate(now)
        if not session.is_open:
            if session.reason == "before-open":
                return self._run_premarket(now, today, session)
            return self._finish(CycleReport(at=now, skipped=session.reason), session, {})

        positions = {position.symbol: position for position in self.repo.positions()}
        universe = self.strategy.universe.symbols
        quote_symbols = sorted(set(universe) | set(positions))
        quotes: dict[str, Quote] = {}
        data_error: str | None = None
        try:
            quotes = dict(self.market_data.quotes(quote_symbols))
            self.kill_switch.note_api_success()
        except Exception as exc:  # noqa: BLE001 - 시세 실패 -> 매수 보류
            data_error = str(exc)
            self._note_api_failure(now, "quotes", exc)
            self.alerter.notify(
                "error",
                "시세 조회 실패 — 매수 보류. 보유 포지션은 가격 판정 불가로 시간청산만 시도합니다",
                error=data_error,
            )

        try:
            self.broker.update_prices({symbol: quote.last_price for symbol, quote in quotes.items()})
            account = self.broker.account()
            self.kill_switch.note_api_success()
        except Exception as exc:  # noqa: BLE001
            self._note_api_failure(now, "account", exc)
            self.alerter.notify("error", "계좌 조회 실패 — 이번 사이클 건너뜀", error=str(exc))
            return self._finish(CycleReport(at=now, skipped="account-error"), session, {})

        equity, cash = account.equity_usd, account.cash_usd
        self.repo.ensure_day_state(today, equity, now)
        self._reconcile(account, today, now)

        # 미체결 주문 동기화 (REST 폴링) — WS 미구현 보완
        self._sync_pending_orders(now, today)
        positions = {position.symbol: position for position in self.repo.positions()}

        report = CycleReport(at=now, equity_usd=equity, cash_usd=cash)
        blocked: list[str] = []

        # ---------------------------------------------------------- (1) Kill Switch
        if self._kill_switch_gate(now, today, equity, blocked):
            report = replace(report, blocked=tuple(dict.fromkeys(blocked)))
            return self._finish(report, session, {})

        # ---------------------------------------------------------- (2) 매도
        intraday_cache: dict[str, list[Bar]] = {}
        sells: list[OrderResult] = list(
            self._flush_premarket_queue(now, today, session, quotes, positions, blocked)
        )
        sells.extend(self._process_exits(positions, quotes, session, now, today, blocked, intraday_cache))
        report = replace(report, sells=tuple(sells))
        if any(result.is_filled for result in sells):
            # 매도 대금이 반영된 잔고로 매수 예산을 다시 계산한다
            try:
                account = self.broker.account()
                equity, cash = account.equity_usd, account.cash_usd
                self.kill_switch.note_api_success()
            except Exception as exc:  # noqa: BLE001
                LOG.warning("매도 후 잔고 재조회 실패: %s", exc)

        # ---------------------------------------------------------- (3) 매수
        inputs: dict[str, SymbolInputs] = {}
        evaluations: dict[str, EntryEvaluation] = {}
        rsi_map: dict[str, Decimal | None] = {}

        allowed, extra_drawdown, gate_reasons = self._buy_gate(now, session, today, equity, data_error)
        blocked.extend(gate_reasons)
        if allowed:
            crashed_today = self._crash_guard_symbols(today)
            for symbol in universe:
                if symbol in positions:
                    continue
                if symbol in crashed_today:
                    blocked.append(f"{symbol}:abnormal-crash")
                    continue
                quote = quotes.get(symbol)
                bars = self._daily_bars(symbol, {})
                check = self.data_guard.check(
                    symbol, quote, bars, now, sma_period=self.strategy.entry.sma_period
                )
                if not check.allowed:
                    blocked.append(f"{symbol}:{check.reason}")
                    continue
                snapshot = self._build_inputs(symbol, quote, bars, intraday_cache)
                inputs[symbol] = snapshot
                rsi_map[symbol] = snapshot.rsi2
                evaluation = evaluate_entry(
                    snapshot,
                    self.strategy.entry,
                    self.strategy.universe,
                    extra_drawdown_pct=extra_drawdown,
                )
                evaluations[symbol] = evaluation
                if evaluation.abnormal_crash:
                    self._note_crash_guard(symbol, today)
                if not evaluation.allowed:
                    blocked.append(f"{symbol}:{evaluation.reason}")
                    continue
                symbol_decision = self._symbol_guards(symbol, today)
                if not symbol_decision.allowed:
                    blocked.append(f"{symbol}:{symbol_decision.reason}")
                    evaluations[symbol] = replace(
                        evaluation, allowed=False, reason=symbol_decision.reason, reasons=evaluation.reasons + (symbol_decision.reason,)
                    )
                    continue
                self.signals.publish(
                    symbol,
                    now,
                    self.strategy.entry.signal_ttl_seconds,
                    evaluation.score,
                    evaluation.threshold_pct,
                )

            changes = self.signals.housekeeping(now, rsi_map)
            for symbol, status in changes:
                blocked.append(f"{symbol}:signal-{status.lower()}")

            valid = {record.symbol: record for record in self.signals.valid(now)}
            candidates = [
                evaluation
                for symbol, evaluation in evaluations.items()
                if evaluation.allowed and symbol in valid
            ]
            purchases = self._process_buys(candidates, valid, account, quotes, positions, now, today, blocked)
            report = replace(report, buys=purchases)
        else:
            self.signals.housekeeping(now, rsi_map)

        report = replace(report, blocked=tuple(dict.fromkeys(blocked)))
        self._log_decisions(report, session, inputs, evaluations, quotes)
        return self._finish(report, session, {})

    # ================================================================== 계좌 Kill Switch
    def _kill_switch_gate(self, now: datetime, today: date, equity: Decimal, blocked: list[str]) -> bool:
        """발동 중이면 True — 신규 주문(매수·매도) 전면 정지."""
        if self.kill_switch.active:
            blocked.append(f"kill-switch:{self.kill_switch.reason()}")
            return True

        day = self.repo.day_state(today)
        tripped = self.kill_switch.check_daily_loss(equity, day.equity_open if day is not None else None, now)
        if tripped:
            blocked.append("risk:daily-loss-limit")
        if self.kill_switch.observe_equity(equity, now):
            tripped = True
        if self._mismatch_today(today):
            state = self.repo.kv_get_json("reconcile_state") or {}
            mismatches = state.get("mismatches") if isinstance(state, dict) else None
            if self.kill_switch.check_mismatch(mismatches, now):
                tripped = True
                blocked.append("reconcile-mismatch")

        if not self.kill_switch.active:
            return False
        if tripped:
            self.alerter.notify(
                "error",
                "계좌 Kill Switch 발동 — 신규 주문 전면 정지(Safe Halt). 원인 확인 후 수동 해제하세요",
                reason=self.kill_switch.reason(),
                detail=dict(self.kill_switch.detail()),
                peak_equity=self.kill_switch.peak_equity(),
                mdd_pct=self.kill_switch.mdd_pct(),
            )
        blocked.append(f"kill-switch:{self.kill_switch.reason()}")
        return True

    def _note_api_failure(self, now: datetime, label: str, exc: Exception) -> None:
        if self.kill_switch.note_api_error(now):
            self.alerter.notify(
                "error",
                "API 연속 오류 — 계좌 Kill Switch 발동(Safe Halt)",
                label=label,
                error=str(exc),
                streak=self.kill_switch.api_error_streak(),
            )

    # ================================================================== 프리마켓
    def _run_premarket(self, now: datetime, today: date, session: SessionEvaluation) -> CycleReport:
        """개장 전 프리마켓 — 소수점 매도 지원 여부에 따라 즉시 매도 또는 09:30~09:32 예약 큐."""
        positions = {position.symbol: position for position in self.repo.positions()}
        report = CycleReport(at=now, skipped=session.reason)
        if not positions:
            return self._finish(report, session, {})
        if self.kill_switch.active:
            return self._finish(
                replace(report, blocked=(f"kill-switch:{self.kill_switch.reason()}",)), session, {}
            )
        quotes: dict[str, Quote] = {}
        try:
            quotes = dict(self.market_data.quotes(sorted(positions)))
            self.kill_switch.note_api_success()
        except Exception as exc:  # noqa: BLE001
            self._note_api_failure(now, "premarket-quotes", exc)
            self.alerter.notify("error", "개장 전 시세 조회 실패 — 예약 매도 판정 불가", error=str(exc))
            return self._finish(report, session, {})

        blocked: list[str] = []
        intraday: dict[str, list[Bar]] = {}
        if self.allow_fractional_premarket_sell:
            sells = self._process_exits(
                positions, quotes, session, now, today, blocked, intraday, premarket=True
            )
            report = replace(report, sells=tuple(sells), blocked=tuple(dict.fromkeys(blocked)))
        else:
            queued = self._queue_premarket_exits(positions, quotes, now, today)
            report = replace(
                report,
                blocked=tuple(dict.fromkeys(blocked)),
                notes=tuple(f"premarket-queued:{symbol}" for symbol in queued),
            )
        return self._finish(report, session, {})

    def _premarket_queue(self) -> list[dict[str, object]]:
        raw = self.repo.kv_get_json(KV_PREMARKET_QUEUE)
        return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    def _save_premarket_queue(self, queue: Sequence[Mapping[str, object]]) -> None:
        self.repo.kv_set_json(KV_PREMARKET_QUEUE, [dict(item) for item in queue])

    def _queue_premarket_exits(
        self,
        positions: Mapping[str, Position],
        quotes: Mapping[str, Quote],
        now: datetime,
        today: date,
    ) -> list[str]:
        queue = self._premarket_queue()
        known = {str(item.get("symbol")) for item in queue}
        queued: list[str] = []
        for symbol in sorted(positions):
            position = positions[symbol]
            quote = quotes.get(symbol)
            if quote is None:
                continue
            trading_days, calendar_days = hold_days(position, today, self.calendar)
            evaluation = evaluate_exit(
                position,
                quote.last_price,
                self.strategy.exit,
                trading_days_held=trading_days,
                calendar_days_held=calendar_days,
                trail_high=self._trail_high(symbol),
            )
            if not evaluation.should_exit or symbol in known:
                continue
            queue.append(
                {
                    "symbol": symbol,
                    "reason": evaluation.reason,
                    "pct": str(evaluation.pct),
                    "queued_at": now.isoformat(),
                    "queue_date": today.isoformat(),
                }
            )
            queued.append(symbol)
            self.alerter.notify(
                "warning",
                "프리마켓 매도 신호 — 09:30~09:32 ET 예약 매도 큐에 등록했습니다",
                symbol=symbol,
                reason=evaluation.reason,
                pct=str(evaluation.pct),
                allow_fractional_premarket_sell=False,
            )
        self._save_premarket_queue(queue)
        return queued

    def _flush_premarket_queue(
        self,
        now: datetime,
        today: date,
        session: SessionEvaluation,
        quotes: Mapping[str, Quote],
        positions: Mapping[str, Position],
        blocked: list[str],
    ) -> tuple[OrderResult, ...]:
        queue = self._premarket_queue()
        if not queue:
            return ()
        window = session.window
        cfg = self.guards_config.session
        in_window = window is not None and window.premarket_flush_allowed(now, cfg.premarket_flush_min)
        if not in_window:
            if window is not None and now >= window.regular_start + timedelta(minutes=cfg.premarket_flush_min):
                self.alerter.notify(
                    "warning",
                    "프리마켓 예약 매도의 집행 창(09:30~09:32 ET)을 놓쳤습니다 — 수동 확인이 필요합니다",
                    symbols=[item.get("symbol") for item in queue],
                )
                blocked.append("premarket-queue-missed")
                self._save_premarket_queue([])
            return ()

        results: list[OrderResult] = []
        remaining: list[dict[str, object]] = []
        for item in queue:
            symbol = str(item.get("symbol"))
            position = positions.get(symbol)
            quote = quotes.get(symbol)
            if position is None:
                continue
            if quote is None:
                remaining.append(item)
                blocked.append(f"{symbol}:quote-missing")
                continue
            reason = str(item.get("reason") or "take_profit")
            if not session.fractional_sell_allowed:
                remaining.append(item)
                blocked.append(f"{symbol}:sell-window-closed")
                continue
            gate = self.rate_guard.check(symbol, "SELL", now)
            if not gate.allowed:
                remaining.append(item)
                blocked.append(f"{symbol}:{gate.reason}")
                continue
            request = OrderRequest(
                symbol=symbol,
                side="SELL",
                client_order_id=self._next_order_id(symbol, PURPOSE_BY_REASON.get(reason, "x"), today),
                qty=qty6(position.qty),
                reason=reason,
                ref_price=quote.last_price,
            )
            result = self._execute(request, now)
            results.append(result)
            if result.status in (ORDER_STATUS_FILLED, ORDER_STATUS_PARTIAL) and result.filled_qty > ZERO:
                self._apply_sell(position, result, now, today, reason)
                self.alerter.notify(
                    "info",
                    "프리마켓 예약 매도 집행",
                    symbol=symbol,
                    reason=reason,
                    qty=str(result.filled_qty),
                    price=str(result.avg_price),
                )
            else:
                blocked.append(f"{symbol}:queued-sell-rejected:{result.error_code}")
        self._save_premarket_queue(remaining)
        return tuple(results)

    # ================================================================== 미체결 동기화
    def _sync_pending_orders(self, now: datetime, today: date) -> None:
        """이전 사이클에 PENDING/PARTIAL 로 남은 주문의 체결분을 원장에 반영한다."""
        for record in self.repo.pending_orders():
            if not record.broker_order_id:
                continue
            try:
                result = self.broker.get_order(record.broker_order_id)
                self.kill_switch.note_api_success()
            except Exception as exc:  # noqa: BLE001
                LOG.warning("미체결 주문 조회 실패 %s: %s", record.broker_order_id, exc)
                self._note_api_failure(now, "order-sync", exc)
                continue
            if result is None:
                continue
            request = OrderRequest(
                symbol=record.symbol,
                side=record.side,
                client_order_id=record.client_order_id,
                amount_usd=record.amount_usd,
                qty=record.qty,
                reason=record.reason,
            )
            self.repo.record_order(request, result, mode=self.mode, created_at=record.created_at)
            delta = result.filled_qty - record.filled_qty
            if delta <= ZERO:
                continue
            if record.side == "BUY":
                self._apply_buy_delta(record.symbol, delta, result.avg_price, now, today, record.reason)
            else:
                position = self.repo.position(record.symbol)
                if position is not None:
                    self._apply_sell_delta(position, delta, now, today, record.reason)
            self.alerter.notify(
                "warning",
                "미체결 주문 체결 동기화",
                symbol=record.symbol,
                side=record.side,
                filled=str(result.filled_qty),
                status=result.status,
            )

    def _apply_buy_delta(
        self,
        symbol: str,
        delta: Decimal,
        price: Decimal | None,
        now: datetime,
        today: date,
        reason: str,
    ) -> None:
        existing = self.repo.position(symbol)
        fill_price = price or (existing.avg_price if existing is not None else None)
        if fill_price is None:
            self.alerter.notify("error", "체결 동기화: 가격 미확인", symbol=symbol)
            return
        if existing is None:
            vix = self._vix_close(today)
            self.repo.save_position(
                Position(
                    symbol=symbol,
                    qty=delta,
                    avg_price=fill_price,
                    entry_at=now,
                    first_entry_date=today,
                    entry_vix=vix,
                    tp_target_pct=vix_take_profit_pct(vix, self.strategy.exit),
                    add_count=0,
                )
            )
        else:
            new_qty = existing.qty + delta
            new_avg = (existing.qty * existing.avg_price + delta * fill_price) / new_qty
            self.repo.save_position(
                replace(
                    existing,
                    qty=new_qty,
                    avg_price=new_avg,
                    add_count=existing.add_count + (1 if reason == "add" else 0),
                    last_add_at=now if reason == "add" else existing.last_add_at,
                )
            )

    def _apply_sell_delta(self, position: Position, delta: Decimal, now: datetime, today: date, reason: str) -> None:
        remaining = position.qty - delta
        if remaining <= MIN_QTY:
            self.repo.delete_position(position.symbol)
            self._clear_trail(position.symbol)
            self._set_exit_cooldown(position.symbol, position, now, today, reason)
        else:
            self.repo.save_position(replace(position, qty=remaining))

    # ================================================================== 매도
    def _process_exits(
        self,
        positions: dict[str, Position],
        quotes: Mapping[str, Quote],
        session: SessionEvaluation,
        now: datetime,
        today: date,
        blocked: list[str],
        intraday_cache: dict[str, list[Bar]],
        *,
        premarket: bool = False,
    ) -> tuple[OrderResult, ...]:
        results: list[OrderResult] = []
        window = session.window
        open_cfg = self.guards_config.session
        for symbol in sorted(positions):
            position = positions[symbol]
            quote = quotes.get(symbol)
            trading_days, calendar_days = hold_days(position, today, self.calendar)
            if quote is None:
                time_due = (
                    trading_days >= self.strategy.exit.time_exit_trading_days
                    or calendar_days >= self.strategy.exit.time_exit_calendar_days
                )
                if not time_due:
                    blocked.append(f"{symbol}:quote-missing-exit-skipped")
                    continue
                # 가격 판정은 불가하지만 시간청산은 강제한다 (시장가 매도)
                if not (session.fractional_sell_allowed or premarket):
                    blocked.append(f"{symbol}:sell-window-closed")
                    continue
                gate = self.rate_guard.check(symbol, "SELL", now)
                if not gate.allowed:
                    blocked.append(f"{symbol}:{gate.reason}")
                    continue
                request = OrderRequest(
                    symbol=symbol,
                    side="SELL",
                    client_order_id=self._next_order_id(symbol, "x", today),
                    qty=qty6(position.qty),
                    reason="time_exit_no_quote",
                    ref_price=position.avg_price,
                )
                result = self._execute(request, now)
                results.append(result)
                if result.status in (ORDER_STATUS_FILLED, ORDER_STATUS_PARTIAL) and result.filled_qty > ZERO:
                    self._apply_sell(position, result, now, today, "time_exit")
                    if position.qty - result.filled_qty <= MIN_QTY:
                        positions.pop(symbol, None)
                    self.alerter.notify(
                        "warning",
                        "시세 없이 시간청산 체결",
                        symbol=symbol,
                        qty=str(result.filled_qty),
                        price=str(result.avg_price),
                    )
                else:
                    blocked.append(f"{symbol}:time-exit-rejected:{result.error_code}")
                continue

            price = quote.last_price
            trail_high = self._trail_high(symbol)
            evaluation = evaluate_exit(
                position,
                price,
                self.strategy.exit,
                trading_days_held=trading_days,
                calendar_days_held=calendar_days,
                trail_high=trail_high,
            )

            if evaluation.arm_trailing:
                self._set_trail_high(symbol, max(price, trail_high or ZERO))
                blocked.append(f"{symbol}:trailing-armed")
                self.alerter.notify(
                    "info",
                    "시간청산 만기 — 상승 추세라 트레일링(-2.0%)으로 전환했습니다",
                    symbol=symbol,
                    pct=str(evaluation.pct),
                    high=str(price),
                )
                continue
            if trail_high is not None and price > trail_high:
                self._set_trail_high(symbol, price)

            if evaluation.should_exit:
                reason = evaluation.reason
                if reason == "time_exit" and self._in_preemptive_window(session, now):
                    reason = "eod_preemptive_time_exit"
            else:
                reason = self._preemptive_reason(position, evaluation.pct, session, now, trading_days, calendar_days)
                if reason is None:
                    continue

            if reason in STOP_FAMILY and position.first_entry_date < today and not premarket:
                if window is not None and not window.overnight_stop_allowed(now, open_cfg.overnight_stop_after_open_min):
                    blocked.append(f"{symbol}:overnight-stop-deferred")
                    continue

            if not (session.fractional_sell_allowed or premarket):
                self.alerter.notify(
                    "error",
                    "청산 신호가 났지만 소수점 매도 마감(15:00 ET) 이후입니다 — 다음 거래일로 이월됩니다",
                    symbol=symbol,
                    reason=reason,
                    pct=str(evaluation.pct),
                )
                blocked.append(f"{symbol}:sell-window-closed")
                continue

            tick = self._tick_guard_check(symbol, price, now, intraday_cache)
            if not tick.allowed:
                detail = {key: value for key, value in dict(tick.detail).items() if key != "symbol"}
                self.alerter.notify(
                    "warning",
                    "이상 틱 가드 발동 — 보조 시세가 확인되지 않아 매도를 보류했습니다(가짜 손절 차단)",
                    symbol=symbol,
                    reason=tick.reason,
                    price=str(price),
                    **detail,
                )
                blocked.append(f"{symbol}:{tick.reason}")
                continue

            gate = self.rate_guard.check(symbol, "SELL", now)
            if not gate.allowed:
                blocked.append(f"{symbol}:{gate.reason}")
                continue
            purpose = PURPOSE_BY_REASON.get(reason, "x")
            request = OrderRequest(
                symbol=symbol,
                side="SELL",
                client_order_id=self._next_order_id(symbol, purpose, today),
                qty=qty6(position.qty),
                reason=reason,
                ref_price=price,
            )
            result = self._execute(request, now)
            results.append(result)
            if result.status in (ORDER_STATUS_FILLED, ORDER_STATUS_PARTIAL) and result.filled_qty > ZERO:
                self._apply_sell(position, result, now, today, reason)
                if position.qty - result.filled_qty <= MIN_QTY:
                    positions.pop(symbol, None)
                self.alerter.notify(
                    "info",
                    "청산 체결",
                    symbol=symbol,
                    reason=reason,
                    pct=str(evaluation.pct),
                    qty=str(result.filled_qty),
                    price=str(result.avg_price),
                )
            elif result.status == ORDER_STATUS_REJECTED:
                self.alerter.notify(
                    "error",
                    "청산 주문 거부 — 포지션 유지, 원장 재대조 필요",
                    symbol=symbol,
                    code=result.error_code,
                    message=result.message,
                )
        return tuple(results)

    def _in_preemptive_window(self, session: SessionEvaluation, now: datetime) -> bool:
        window = session.window
        if window is None:
            return False
        cfg = self.guards_config.session
        return window.in_preemptive_window(
            now, cfg.fractional_close_buffer_min, cfg.preemptive_start_min, cfg.preemptive_end_min
        )

    def _preemptive_reason(
        self,
        position: Position,
        pct: Decimal,
        session: SessionEvaluation,
        now: datetime,
        trading_days: int,
        calendar_days: int,
    ) -> str | None:
        """14:50~14:58 ET 선제 청산 — 근접 손절 / 만기 종목."""
        if not self._in_preemptive_window(session, now):
            return None
        near = preemptive_near_stop_pct(position, self.strategy.exit)
        if pct <= -near:
            return "eod_preemptive_stop"
        if (
            trading_days >= self.strategy.exit.time_exit_trading_days
            or calendar_days >= self.strategy.exit.time_exit_calendar_days
        ):
            return "eod_preemptive_time_exit"
        return None

    def _tick_guard_check(
        self, symbol: str, price: Decimal, now: datetime, intraday_cache: dict[str, list[Bar]]
    ) -> GuardDecision:
        bars = intraday_cache.get(symbol)
        if bars is None:
            try:
                bars = list(self.market_data.intraday_bars(symbol, minutes=60))
            except Exception as exc:  # noqa: BLE001
                LOG.warning("1분봉 조회 실패 %s: %s", symbol, exc)
                bars = []
            intraday_cache[symbol] = bars
        crosscheck = None
        if self.tick_crosscheck is not None:
            crosscheck = self._crosscheck_price
        return self.tick_guard.check(symbol, price, bars, now, crosscheck)

    def _crosscheck_price(self, symbol: str) -> Decimal | None:
        if self.tick_crosscheck is None:
            return None
        try:
            return self.tick_crosscheck(symbol)
        except Exception:  # noqa: BLE001 - 보조 소스 실패는 미확인
            return None

    def _trail_high(self, symbol: str) -> Decimal | None:
        value = self.repo.kv_get_json(f"trail:{symbol}")
        if not isinstance(value, dict):
            return None
        raw = value.get("high")
        return None if raw is None else dec(raw)

    def _set_trail_high(self, symbol: str, high: Decimal) -> None:
        self.repo.kv_set_json(f"trail:{symbol}", {"high": str(high)})

    def _clear_trail(self, symbol: str) -> None:
        self.repo.kv_set_json(f"trail:{symbol}", {})

    def _apply_sell(
        self, position: Position, result: OrderResult, now: datetime, today: date, reason: str = "exit"
    ) -> None:
        remaining = position.qty - result.filled_qty
        if remaining <= MIN_QTY:
            self.repo.delete_position(position.symbol)
            self._clear_trail(position.symbol)
            self._set_exit_cooldown(position.symbol, position, now, today, reason)
            if position.first_entry_date == today:
                # 당일 매수 후 당일 매도 = 데이트레이드
                self.account_guard.record_day_trade(position.symbol, today, now)
        else:
            self.repo.save_position(replace(position, qty=remaining))
        if result.filled_qty > ZERO:
            proceeds = (result.filled_amount or ZERO) - result.commission - result.tax
            self.account_guard.record_sell(position.symbol, proceeds, today)

    def _set_exit_cooldown(
        self, symbol: str, position: Position, now: datetime, today: date, reason: str
    ) -> None:
        cfg = self.strategy.exit
        if reason in STOP_FAMILY and cfg.stop_loss_eod_cooldown:
            window = self.calendar.session_on(today)
            until = window.regular_end if window is not None else now + timedelta(seconds=cfg.cooldown_seconds)
            if until <= now:
                until = now + timedelta(seconds=cfg.cooldown_seconds)
            self.cooldowns.block_until(symbol, until, REASON_STOP_LOSS_EOD)
            self.alerter.notify(
                "warning",
                "손절 후 당일 재진입 금지(EOD 쿨다운) — 다음 거래일에 해제됩니다",
                symbol=symbol,
                until=until.isoformat(),
            )
        else:
            self.cooldowns.block(symbol, now, "exit")

    # ================================================================== 매수
    def _buy_gate(
        self,
        now: datetime,
        session: SessionEvaluation,
        today: date,
        equity: Decimal,
        data_error: str | None,
    ) -> tuple[bool, Decimal, list[str]]:
        reasons: list[str] = []
        if self.halt is not None and self.halt.halted:
            reasons.append(f"kill-switch:{self.halt.reason()}")
        if not session.entry_allowed:
            reasons.append(f"session:{session.reason}")
        if self._mismatch_today(today):
            reasons.append("reconcile-mismatch")
        if data_error is not None:
            reasons.append("quotes-unavailable")
        risk = self.risk_guard.check(today, equity, now)
        if not risk.allowed:
            reasons.append(f"risk:{risk.reason}")
        pdt = self.account_guard.pdt_status(today, equity)
        if not pdt.allowed:
            reasons.append(f"account:{pdt.reason}")
        market = self.market_guard.check()
        if not market.allowed:
            reasons.append(f"market:{market.reason}")
        macro = self.macro_guard.decision()
        if not macro.allowed:
            reasons.append(f"macro:{macro.reason}")
        if reasons:
            return False, ZERO, reasons
        return True, self.macro_guard.drawdown_boost_pct(today), []

    def _process_buys(
        self,
        candidates: Sequence[EntryEvaluation],
        valid_signals: Mapping[str, object],
        account: AccountState,
        quotes: Mapping[str, Quote],
        positions: dict[str, Position],
        now: datetime,
        today: date,
        blocked: list[str],
    ) -> tuple[OrderResult, ...]:
        if not candidates and not positions:
            return ()
        equity, cash = account.equity_usd, account.cash_usd
        # 현금 계좌는 미결제 매도대금(T+1)을 뺀 결제 완료 현금만 쓴다 (GFV 방지)
        settled = self.account_guard.settled_cash(cash, today)
        budget = Budget(spendable_cash(equity, settled, self.strategy.capital))
        results: list[OrderResult] = []

        # 물타기: 보유 종목이 대상 (슬롯을 추가로 쓰지 않는다)
        for symbol, position in list(positions.items()):
            if symbol not in self.strategy.universe.weights:
                continue
            quote = quotes.get(symbol)
            if quote is None:
                continue
            daily = self._daily_bars(symbol, {})
            rsi = wilder_rsi([bar.close for bar in daily], self.strategy.entry.rsi_period) if daily else None
            atr = atr_pct(daily, self.strategy.entry.atr_period) if daily else None
            evaluation = evaluate_add(
                position,
                self.strategy.add,
                last_price=quote.last_price,
                rsi2=rsi,
                atr_pct=atr,
                now=now,
            )
            if not evaluation.allowed:
                blocked.append(f"{symbol}:add-{evaluation.reason}")
                continue
            if self.cooldowns.is_blocked(symbol, now):
                blocked.append(f"{symbol}:cooldown")
                continue
            gate = self.rate_guard.check(symbol, "BUY", now)
            if not gate.allowed:
                blocked.append(f"{symbol}:{gate.reason}")
                continue
            amount = plan_add(position, quote.last_price, equity, settled, self.strategy.capital, budget)
            if amount is None:
                blocked.append(f"{symbol}:add-cap-or-min-order")
                continue
            duplicate = self.kill_switch.note_order_attempt(symbol, "BUY", now)
            if not duplicate.allowed:
                blocked.append(f"{symbol}:{duplicate.reason}")
                self.alerter.notify(
                    "error", "5분 내 동일 종목 중복 주문 시도 — 계좌 Kill Switch 발동", symbol=symbol, side="BUY"
                )
                break
            request = OrderRequest(
                symbol=symbol,
                side="BUY",
                client_order_id=self._next_order_id(symbol, "a", today),
                amount_usd=amount,
                reason="add",
                ref_price=quote.last_price,
            )
            result = self._execute(request, now)
            results.append(result)
            if result.status in (ORDER_STATUS_FILLED, ORDER_STATUS_PARTIAL) and result.filled_qty > ZERO:
                updated = self._apply_add(position, result, now)
                positions[symbol] = updated
                cash = self._cash_after(cash, result, amount)
                settled = self.account_guard.settled_cash(cash, today)
                self.alerter.notify(
                    "info", "물타기 체결", symbol=symbol, amount=str(amount), qty=str(result.filled_qty),
                    price=str(result.avg_price), add_count=position.add_count + 1,
                )
            elif result.status == ORDER_STATUS_REJECTED:
                blocked.append(f"{symbol}:add-rejected:{result.error_code}")

        # 신규 진입: 점수 상위부터 빈 슬롯만큼
        slots = free_slots(self.strategy.capital.slots, positions)
        ranked = rank_candidates(candidates, self.strategy.universe)
        for evaluation in ranked:
            if slots <= 0:
                break
            symbol = evaluation.symbol
            if self.cooldowns.is_blocked(symbol, now):
                reason = self.cooldowns.reason(symbol) or "exit"
                blocked.append(f"{symbol}:cooldown-{reason}")
                continue
            if self.account_guard.rebuy_blocked(symbol, today):
                blocked.append(f"{symbol}:gfv-unsettled-funds")
                continue
            quote = quotes.get(symbol)
            if quote is None:
                blocked.append(f"{symbol}:quote-missing")
                continue
            gate = self.rate_guard.check(symbol, "BUY", now)
            if not gate.allowed:
                blocked.append(f"{symbol}:{gate.reason}")
                continue
            amount = plan_new_entry(equity, settled, self.strategy.capital, budget)
            if amount is None:
                blocked.append(f"{symbol}:no-cash-or-min-order")
                break
            duplicate = self.kill_switch.note_order_attempt(symbol, "BUY", now)
            if not duplicate.allowed:
                blocked.append(f"{symbol}:{duplicate.reason}")
                self.alerter.notify(
                    "error", "5분 내 동일 종목 중복 주문 시도 — 계좌 Kill Switch 발동", symbol=symbol, side="BUY"
                )
                break
            request = OrderRequest(
                symbol=symbol,
                side="BUY",
                client_order_id=self._next_order_id(symbol, "e", today),
                amount_usd=amount,
                reason="entry",
                ref_price=quote.last_price,
            )
            result = self._execute(request, now)
            results.append(result)
            if result.status in (ORDER_STATUS_FILLED, ORDER_STATUS_PARTIAL) and result.filled_qty > ZERO:
                self._open_position(symbol, result, now, today)
                cash = self._cash_after(cash, result, amount)
                settled = self.account_guard.settled_cash(cash, today)
                slots -= 1
                signal = valid_signals.get(symbol)
                if signal is not None:
                    self.signals.consume(signal.id)  # type: ignore[attr-defined]
                self.alerter.notify(
                    "info",
                    "신규 진입 체결",
                    symbol=symbol,
                    score=str(evaluation.score),
                    drawdown_pct=str(evaluation.drawdown_pct),
                    amount=str(amount),
                    qty=str(result.filled_qty),
                    price=str(result.avg_price),
                )
            elif result.status == ORDER_STATUS_DRY_RUN:
                slots -= 1
            elif result.status == ORDER_STATUS_REJECTED:
                blocked.append(f"{symbol}:entry-rejected:{result.error_code}")
                if result.error_code in ("insufficient-buying-power", "order-hours-closed"):
                    break
        return tuple(results)

    def _open_position(self, symbol: str, result: OrderResult, now: datetime, today: date) -> None:
        vix = self._vix_close(today)
        target = vix_take_profit_pct(vix, self.strategy.exit)
        position = Position(
            symbol=symbol,
            qty=result.filled_qty,
            avg_price=result.avg_price or ZERO,
            entry_at=now,
            first_entry_date=today,
            entry_vix=vix,
            tp_target_pct=target,
            add_count=0,
        )
        self.repo.save_position(position)
        self._clear_trail(symbol)
        LOG.info(
            "포지션 개설 %s qty=%s avg=%s VIX=%s 목표익절=%s%%",
            symbol, position.qty, position.avg_price, vix, target,
        )

    def _apply_add(self, position: Position, result: OrderResult, now: datetime) -> Position:
        filled = result.filled_qty
        price = result.avg_price or position.avg_price
        new_qty = position.qty + filled
        new_avg = (position.qty * position.avg_price + filled * price) / new_qty if new_qty > ZERO else position.avg_price
        updated = replace(
            position,
            qty=new_qty,
            avg_price=new_avg,
            add_count=position.add_count + 1,
            last_add_at=now,
            # tp_target_pct 는 유지 (물타기 시 목표 익절률 불변)
        )
        self.repo.save_position(updated)
        return updated

    # ================================================================== 보조
    def _symbol_guards(self, symbol: str, today: date) -> GuardDecision:
        if self.earnings_guard is not None:
            decision = self.earnings_guard.decisions([symbol], today)[symbol]
            if not decision.allowed:
                return decision
        if self.filings_guard is not None:
            decision = self.filings_guard.decisions([symbol], today)[symbol]
            if not decision.allowed:
                return decision
        if self.news_guard is not None:
            decision = self.news_guard.decisions([symbol])[symbol]
            if not decision.allowed:
                return decision
        return GuardDecision.ok()

    def _crash_guard_symbols(self, today: date) -> set[str]:
        state = self.repo.kv_get_json(KV_CRASH_GUARD)
        if not isinstance(state, dict) or state.get("date") != today.isoformat():
            return set()
        return {str(symbol) for symbol in state.get("symbols") or []}

    def _note_crash_guard(self, symbol: str, today: date) -> None:
        symbols = self._crash_guard_symbols(today)
        if symbol in symbols:
            return
        symbols.add(symbol)
        self.repo.kv_set_json(KV_CRASH_GUARD, {"date": today.isoformat(), "symbols": sorted(symbols)})
        self.alerter.notify(
            "warning",
            "비정상 급락 가드 — 당일 신규 매수 대상에서 제외했습니다",
            symbol=symbol,
            rule="장중 고점 대비 -3.0 x ATR(20)% 초과",
        )

    def _build_inputs(
        self,
        symbol: str,
        quote: Quote,
        daily: Sequence[Bar],
        intraday_cache: dict[str, list[Bar]],
    ) -> SymbolInputs:
        closes = [bar.close for bar in daily]
        rsi = wilder_rsi(closes, self.strategy.entry.rsi_period) if closes else None
        atr = atr_pct(daily, self.strategy.entry.atr_period) if daily else None
        sma200 = sma(closes, self.strategy.entry.sma_period)
        if symbol not in intraday_cache:
            try:
                intraday_cache[symbol] = list(self.market_data.intraday_bars(symbol, minutes=390))
            except Exception as exc:  # noqa: BLE001
                LOG.warning("1분봉 조회 실패 %s: %s", symbol, exc)
                intraday_cache[symbol] = []
        hod = robust_intraday_high(
            intraday_cache[symbol], enabled=self.strategy.entry.hod_outlier_filter
        )
        if hod is None or hod < quote.last_price:
            hod = quote.last_price
        return SymbolInputs(
            symbol=symbol,
            last_price=quote.last_price,
            hod=hod,
            rsi2=rsi,
            atr_pct=atr,
            sma200=sma200,
            quote_time=quote.timestamp,
        )

    def _daily_bars(self, symbol: str, cache: dict[str, list[Bar]]) -> list[Bar]:
        if symbol in cache:
            return cache[symbol]
        try:
            bars = list(self.market_data.daily_bars(symbol, count=max(self.strategy.entry.sma_period + 20, 220)))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("일봉 조회 실패 %s: %s", symbol, exc)
            bars = []
        cache[symbol] = bars
        return bars

    def _vix_close(self, today: date) -> Decimal | None:
        if self._vix_date == today:
            return self._vix_value
        value: Decimal | None = None
        try:
            payload = self.market_data.last_daily_close(self.guards_config.market.vix_symbol)
            value = None if payload is None else dec(payload[0])
        except Exception as exc:  # noqa: BLE001
            self.alerter.notify("warning", "VIX 확인 실패 — 목표 익절 +5% 적용", error=str(exc))
        self._vix_date = today
        self._vix_value = value
        return value

    def _execute(self, request: OrderRequest, now: datetime) -> OrderResult:
        existing = self.repo.order_by_client_id(request.client_order_id)
        if existing is not None and existing.status not in (ORDER_STATUS_REJECTED, "CANCELED"):
            return OrderResult(
                client_order_id=existing.client_order_id,
                status=existing.status,
                broker_order_id=existing.broker_order_id,
                filled_qty=existing.filled_qty,
                avg_price=existing.avg_price,
                filled_amount=existing.filled_amount,
                commission=existing.commission,
                tax=existing.tax,
                filled_at=existing.filled_at,
                error_code=existing.error_code,
                message=existing.message,
            )
        try:
            result = self.broker.place_order(request)
            self.kill_switch.note_api_success()
        except LiveOrdersDisabled:
            raise
        except Exception as exc:  # noqa: BLE001 - 브로커 오류는 주문 거부로 기록
            code = getattr(exc, "code", "broker-error")
            result = OrderResult(
                client_order_id=request.client_order_id,
                status=ORDER_STATUS_REJECTED,
                error_code=code,
                message=str(exc),
                filled_at=now,
            )
            self.alerter.notify("error", "주문 실패", symbol=request.symbol, side=request.side, code=code, message=str(exc))
            if code == "broker-error":
                self._note_api_failure(now, "order", exc)
        self.repo.record_order(request, result, mode=self.mode, created_at=now)
        return result

    def _next_order_id(self, symbol: str, purpose: str, today: date) -> str:
        sequence = int(self.repo.kv_get("order_seq", "0") or "0") + 1
        self.repo.kv_set("order_seq", str(sequence))
        return f"as-{today.strftime('%Y%m%d')}-{symbol}-{purpose}{sequence:03d}"

    def _reconcile(self, account: AccountState, today: date, now: datetime, force: bool = False) -> None:
        state = self.repo.kv_get_json("reconcile_state")
        if not force and isinstance(state, dict) and state.get("date") == today.isoformat():
            return
        result = reconcile(
            self.repo.positions(), account.positions, self.guards_config.risk.reconcile_qty_tolerance
        )
        self.repo.kv_set_json(
            "reconcile_state",
            {"date": today.isoformat(), "ok": result.ok, "mismatches": list(result.mismatches), "at": now.isoformat()},
        )
        if not result.ok:
            self.alerter.notify(
                "error",
                "보유 수량 불일치 — 신규 매수 중단, 매도만 허용",
                mismatches=list(result.mismatches),
            )

    def _mismatch_today(self, today: date) -> bool:
        state = self.repo.kv_get_json("reconcile_state")
        return isinstance(state, dict) and state.get("date") == today.isoformat() and not state.get("ok", True)

    def _cash_after(self, cash: Decimal, result: OrderResult, requested: Decimal) -> Decimal:
        spent = result.filled_amount if result.filled_amount is not None else requested
        return cash - spent - result.commission

    @staticmethod
    def _symbol_of(result: OrderResult) -> str | None:
        parts = result.client_order_id.split("-")
        return parts[2] if len(parts) >= 3 else None

    def _finish(self, report: CycleReport, session: SessionEvaluation, extra: Mapping[str, object]) -> CycleReport:
        LOG.info(
            "cycle %s | session=%s | equity=%s cash=%s | sells=%d buys=%d | blocked=%s",
            report.at.isoformat(),
            session.reason or "open",
            report.equity_usd,
            report.cash_usd,
            len(report.sells),
            len(report.buys),
            ",".join(report.blocked) or "-",
        )
        return report

    def _log_decisions(
        self,
        report: CycleReport,
        session: SessionEvaluation,
        inputs: Mapping[str, SymbolInputs],
        evaluations: Mapping[str, EntryEvaluation],
        quotes: Mapping[str, Quote],
    ) -> None:
        record = {
            "at": report.at.isoformat(),
            "mode": self.mode,
            "session": session.to_dict(),
            "equity_usd": str(report.equity_usd),
            "cash_usd": str(report.cash_usd),
            "blocked": list(report.blocked),
            "kill_switch": self.kill_switch.describe(),
            "quotes": {
                symbol: {"last": str(quote.last_price), "ts": None if quote.timestamp is None else quote.timestamp.isoformat()}
                for symbol, quote in quotes.items()
            },
            "symbols": {
                symbol: {
                    "rsi2": None if item.rsi2 is None else str(item.rsi2),
                    "atr_pct": None if item.atr_pct is None else str(item.atr_pct),
                    "sma200": None if item.sma200 is None else str(item.sma200),
                    "hod": None if item.hod is None else str(item.hod),
                    "evaluation": evaluations[symbol].to_dict() if symbol in evaluations else None,
                }
                for symbol, item in inputs.items()
            },
            "orders": [
                {
                    "client_order_id": result.client_order_id,
                    "status": result.status,
                    "filled_qty": str(result.filled_qty),
                    "avg_price": None if result.avg_price is None else str(result.avg_price),
                    "error": result.error_code,
                }
                for result in report.orders
            ],
        }
        self.decision_log.write(record)
