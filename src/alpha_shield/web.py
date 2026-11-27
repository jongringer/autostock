"""봇 내장 웹 대시보드 — 표준 라이브러리 `http.server` 만 쓴다.

`python -m alpha_shield --web` 로 뜨고, 기본은 127.0.0.1:8080 (로컬 전용).

- `GET  /`               : 한글 반응형 대시보드 (크고 선명한 글씨)
- `GET  /api/status`     : 요약·방어막·8종목 판정·보유 슬롯·주문/알림 로그
- `POST /api/control`    : 시작(dry-run/paper) · 정지 · 킬스위치 ON/OFF
- `POST /api/run-once`   : 사이클 1회 즉시 실행

안전 원칙은 CLI 와 같다. 실주문은 `LIVE_ORDERS=true` 환경변수와 `state/ARMED` 파일이
모두 있을 때만 열리고, 웹의 live 버튼도 그 두 조건이 충족될 때만 활성화된다.
웹은 ARMED 파일을 만들지 않는다.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

from . import __version__
from .broker.dryrun import DryRunBroker
from .broker.paper import PaperBroker
from .calendar import SessionGuard, StaticUsCalendar
from .clock import ET, et_date, now_et, to_kst
from .config import ConfigError, GuardsConfig, Settings, StrategyConfig
from .indicators import atr_pct, robust_intraday_high, sma, wilder_rsi
from .guards.account import AccountRegulationGuard
from .marketdata.macro import MacroCalendarError, load_macro_calendar
from .models import Bar, CycleReport, Position, Quote
from .numeric import ZERO, dec, pct4
from .safety import AccountKillSwitch, HaltSwitch, LiveOrdersDisabled, SafetyLock
from .state.repo import Repo
from .strategy.entry import SymbolInputs, entry_threshold_pct, evaluate_entry
from .strategy.exit import hold_days, stop_loss_pct
from .strategy.slots import free_slots
from .webui import PAGE

LOG = logging.getLogger(__name__)

STATUS_REASONS = {
    "ok": "진입 조건 충족",
    "no-dip": "눌림 부족",
    "rsi-too-high": "RSI(2) 높음",
    "chase-block": "추격 차단(RSI≥70)",
    "below-sma-floor": "SMA200×0.95 미달",
    "abnormal-crash": "비정상 급락(당일 매수 제외)",
    "rsi-unavailable": "RSI 결측",
    "sma200-unavailable": "SMA200 결측",
    "hod-unavailable": "장중고점 결측",
    "invalid-price": "가격 이상",
    "quote-missing": "시세 없음",
    "quote-stale": "시세 지연",
    "quote-timestamp-missing": "시세 시각 없음",
    "daily-bars-stale": "일봉 지연",
    "no-data": "데이터 없음",
}

SESSION_LABELS = {
    "market-holiday": "휴장일",
    "before-open": "개장 전",
    "after-close": "장 마감",
    "opening-grace": "개장 유예 (매수 금지)",
    "closing-grace": "마감 유예 (청산만)",
    "": "정규장 · 매수 가능",
}

LIVE_LOCK_HINT = "LIVE_ORDERS=true 환경변수와 state/ARMED 파일이 모두 있어야 실주문이 열립니다."


def _s(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value.quantize(Decimal("0.0001")).normalize())
    return str(value)


def _money(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(Decimal("0.01")))


def _num(value: Decimal | None, digits: int = 2) -> str | None:
    """화면 표시용 숫자 — 자릿수를 줄여 노안에도 읽히게 한다."""
    if value is None:
        return None
    quantum = Decimal(1).scaleb(-digits)
    return str(value.quantize(quantum))


def _kst(value: datetime | None, fmt: str = "%m-%d %H:%M:%S") -> str | None:
    if value is None:
        return None
    try:
        return to_kst(value).strftime(fmt)
    except (ValueError, OverflowError):
        return None


def _et(value: datetime | None, fmt: str = "%m-%d %H:%M ET") -> str | None:
    if value is None:
        return None
    try:
        return value.astimezone(ET).strftime(fmt)
    except (ValueError, OverflowError):
        return None


# ====================================================================== 시세 스냅샷
@dataclass
class MarketSnapshot:
    """대시보드가 쓰는 시세 스냅샷. 한 스레드가 채우고 요청 스레드는 읽기만 한다."""

    started_at: datetime | None = None
    updated_at: datetime | None = None
    quotes: dict[str, Quote] = field(default_factory=dict)
    changes: dict[str, Decimal] = field(default_factory=dict)
    daily: dict[str, list[Bar]] = field(default_factory=dict)
    intraday: dict[str, list[Bar]] = field(default_factory=dict)
    nq_change_pct: Decimal | None = None
    vix: Decimal | None = None
    errors: list[str] = field(default_factory=list)
    complete: bool = False
    fetching: bool = False

    def copy(self) -> "MarketSnapshot":
        return MarketSnapshot(
            started_at=self.started_at,
            updated_at=self.updated_at,
            quotes=dict(self.quotes),
            changes=dict(self.changes),
            daily={key: list(value) for key, value in self.daily.items()},
            intraday={key: list(value) for key, value in self.intraday.items()},
            nq_change_pct=self.nq_change_pct,
            vix=self.vix,
            errors=list(self.errors),
            complete=self.complete,
            fetching=self.fetching,
        )


class SnapshotFeed:
    """백그라운드에서 시세·지표 원천을 모아 두는 공급자.

    Yahoo 호출은 1초 간격 스로틀이 있어 8종목을 한 번에 받으면 20~30초 걸린다.
    그래서 받는 즉시 하나씩 공개(publish)해 화면이 먼저 뜨도록 한다.
    """

    def __init__(self, market_data, symbols: Sequence[str], *, refresh_seconds: float = 60.0, clock=now_et) -> None:
        self.market_data = market_data
        self.symbols = tuple(dict.fromkeys(symbols))
        self.refresh_seconds = max(15.0, float(refresh_seconds))
        self.clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot = MarketSnapshot()
        self._bar_count = 260

    # -- 읽기 ---------------------------------------------------------
    def snapshot(self) -> MarketSnapshot:
        with self._lock:
            return self._snapshot.copy()

    # -- 쓰기 ---------------------------------------------------------
    def _publish(self, mutate: Callable[[MarketSnapshot], None]) -> None:
        with self._lock:
            mutate(self._snapshot)
            self._snapshot.updated_at = self.clock()

    def refresh_once(self) -> MarketSnapshot:
        self._publish(lambda snap: self._begin(snap))
        try:
            self._collect()
        except Exception as exc:  # noqa: BLE001 - 대시보드가 죽지 않게
            LOG.warning("시세 스냅샷 수집 실패: %s", exc)
            self._publish(lambda snap: snap.errors.append(str(exc)))
        return self.snapshot()

    def _begin(self, snap: MarketSnapshot) -> None:
        snap.started_at = self.clock()
        snap.fetching = True
        snap.complete = False
        snap.errors = []

    def _collect(self) -> None:
        for symbol in self.symbols:
            self._fetch_quote(symbol)
        for symbol in self.symbols:
            self._fetch_daily(symbol)
        for symbol in self.symbols:
            self._fetch_intraday(symbol)
        self._fetch_indices()

        def finish(snap: MarketSnapshot) -> None:
            snap.complete = True
            snap.fetching = False

        self._publish(finish)

    def _note_error(self, message: str) -> None:
        LOG.debug("스냅샷 경고: %s", message)
        self._publish(lambda snap: snap.errors.append(message))

    def _fetch_quote(self, symbol: str) -> None:
        try:
            quotes = self.market_data.quotes([symbol])
        except Exception as exc:  # noqa: BLE001
            self._note_error(f"{symbol}: 시세 실패 ({exc})")
            return
        quote = quotes.get(symbol) if isinstance(quotes, Mapping) else None
        if quote is None:
            self._note_error(f"{symbol}: 시세 없음")
            return

        def apply(snap: MarketSnapshot) -> None:
            snap.quotes[symbol] = quote
            change = self._change_pct(snap, symbol, quote.last_price)
            if change is not None:
                snap.changes[symbol] = change

        self._publish(apply)

    def _change_pct(self, snap: MarketSnapshot, symbol: str, last: Decimal) -> Decimal | None:
        """최근 거래일 등락률(%).

        일봉의 마지막 봉이 오늘(장중) 봉이면 그 전 봉 종가가 기준이고,
        장이 끝나 시세가 마지막 종가와 같을 때도 그 세션의 등락률을 보여 준다.
        """
        bars = snap.daily.get(symbol) or []
        if not bars:
            return None
        previous = bars[-1].close
        tolerance = max(Decimal("0.01"), last * Decimal("0.0002"))
        if len(bars) >= 2 and abs(bars[-1].close - last) <= tolerance:
            previous = bars[-2].close
        if previous <= ZERO:
            return None
        return pct4((last - previous) / previous * Decimal(100))

    def _fetch_daily(self, symbol: str) -> None:
        try:
            bars = list(self.market_data.daily_bars(symbol, count=self._bar_count))
        except Exception as exc:  # noqa: BLE001
            self._note_error(f"{symbol}: 일봉 실패 ({exc})")
            return
        def apply(snap: MarketSnapshot) -> None:
            snap.daily[symbol] = bars
            quote = snap.quotes.get(symbol)
            if quote is not None:
                change = self._change_pct(snap, symbol, quote.last_price)
                if change is not None:
                    snap.changes[symbol] = change

        self._publish(apply)

    def _fetch_intraday(self, symbol: str) -> None:
        try:
            bars = list(self.market_data.intraday_bars(symbol, minutes=390))
        except Exception as exc:  # noqa: BLE001
            self._note_error(f"{symbol}: 1분봉 실패 ({exc})")
            return
        self._publish(lambda snap: snap.intraday.__setitem__(symbol, bars))

    def _fetch_indices(self) -> None:
        nq = None
        try:
            nq = self.market_data.futures_change_pct("NQ=F")
        except Exception as exc:  # noqa: BLE001
            self._note_error(f"NQ: {exc}")
        vix = None
        try:
            vix = self.market_data.last_price("^VIX")
        except Exception as exc:  # noqa: BLE001
            self._note_error(f"VIX: {exc}")
        self._publish(lambda snap: (setattr(snap, "nq_change_pct", nq), setattr(snap, "vix", vix)))

    # -- 생명주기 ------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="snapshot-feed", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh_once()
            except Exception as exc:  # noqa: BLE001
                LOG.warning("스냅샷 루프 예외: %s", exc)
            if self._stop.wait(self.refresh_seconds):
                break

    def stop(self) -> None:
        self._stop.set()


# ====================================================================== 실행 루프
@dataclass
class RunnerState:
    state: str = "stopped"  # stopped | running | stopping
    mode: str | None = None
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    cycles: int = 0
    last_cycle_at: datetime | None = None
    last_error: str | None = None
    last_summary: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self, interval: int) -> dict[str, Any]:
        return {
            "state": self.state,
            "mode": self.mode,
            "interval_seconds": interval,
            "cycles": self.cycles,
            "started_at": _kst(self.started_at),
            "stopped_at": _kst(self.stopped_at),
            "last_cycle_at": _kst(self.last_cycle_at),
            "last_error": self.last_error,
            "last_summary": dict(self.last_summary),
        }


class Runner:
    """엔진을 만들어 백그라운드 스레드에서 사이클을 돌린다 (웹 버튼이 제어)."""

    def __init__(
        self,
        engine_factory: Callable[[str], Any],
        *,
        interval: int = 300,
        clock=now_et,
    ) -> None:
        self.engine_factory = engine_factory
        self.interval = max(5, int(interval))
        self.clock = clock
        self.state = RunnerState()
        self._engines: dict[str, Any] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._cycle_lock = threading.Lock()
        self._lock = threading.Lock()

    # -- 엔진 ---------------------------------------------------------
    def engine(self, mode: str):
        if mode not in self._engines:
            engine = self.engine_factory(mode)
            engine.bootstrap()
            self._engines[mode] = engine
        return self._engines[mode]

    def broker(self, mode: str | None = None):
        mode = mode or self.state.mode
        engine = self._engines.get(mode) if mode else None
        return getattr(engine, "broker", None) if engine is not None else None

    # -- 사이클 -------------------------------------------------------
    def run_cycle(self, mode: str) -> Mapping[str, Any]:
        with self._cycle_lock:
            engine = self.engine(mode)
            report: CycleReport = engine.run_cycle()
            summary = summarize_report(report)
            self.state.mode = mode
            self.state.cycles += 1
            self.state.last_cycle_at = report.at
            self.state.last_summary = summary
            self.state.last_error = None
            return summary

    def start(self, mode: str) -> None:
        with self._lock:
            if self.state.state == "running":
                raise RuntimeError("이미 실행 중입니다")
            engine = self.engine(mode)  # 실패하면 여기서 예외 (live 잠금 포함)
            self._stop.clear()
            self.state.state = "running"
            self.state.mode = mode
            self.state.started_at = self.clock()
            self.state.stopped_at = None
            self.state.last_error = None
            self._thread = threading.Thread(target=self._loop, args=(engine, mode), name="alpha-shield-web", daemon=True)
            self._thread.start()

    def _loop(self, engine, mode: str) -> None:
        while not self._stop.is_set():
            try:
                self.run_cycle(mode)
            except LiveOrdersDisabled as exc:
                self.state.last_error = f"실주문 잠금: {exc}"
                break
            except Exception as exc:  # noqa: BLE001 - 한 사이클 실패가 루프를 죽이지 않게
                LOG.exception("사이클 실패")
                self.state.last_error = str(exc)
                self.state.cycles += 1
                self.state.last_cycle_at = self.clock()
            if self._stop.wait(self.interval):
                break
        with self._lock:
            self.state.state = "stopped"
            self.state.stopped_at = self.clock()

    def stop(self, timeout: float = 5.0) -> bool:
        thread = self._thread
        self._stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                self.state.state = "stopping"
                return False
        self.state.state = "stopped"
        self.state.stopped_at = self.clock()
        return True

    @property
    def running(self) -> bool:
        return self.state.state == "running"


def summarize_report(report: CycleReport) -> dict[str, Any]:
    def order(result) -> dict[str, Any]:
        return {
            "client_order_id": result.client_order_id,
            "status": result.status,
            "filled_qty": _s(result.filled_qty) or "0",
            "avg_price": _s(result.avg_price),
            "error": result.error_code,
        }

    return {
        "at": report.at.isoformat(),
        "at_kst": _kst(report.at),
        "skipped": report.skipped,
        "equity_usd": _money(report.equity_usd),
        "cash_usd": _money(report.cash_usd),
        "orders": len(report.orders),
        "sells": [order(item) for item in report.sells],
        "buys": [order(item) for item in report.buys],
        "blocked": list(report.blocked)[:40],
    }


# ====================================================================== 웹 앱
class WebApp:
    def __init__(
        self,
        *,
        settings: Settings,
        strategy: StrategyConfig,
        guards: GuardsConfig,
        repo_factory: Callable[[], Repo],
        market_data,
        engine_factory: Callable[[str], Any],
        calendar=None,
        clock=now_et,
        interval: int = 300,
        refresh_seconds: float = 60.0,
        token: str | None = None,
    ) -> None:
        self.settings = settings
        self.strategy = strategy
        self.guards = guards
        self.repo_factory = repo_factory
        self.market_data = market_data
        self.clock = clock
        self.token = token or secrets.token_urlsafe(24)
        self.calendar = calendar or StaticUsCalendar(settings.root / "config" / "market_calendar_2026.json")
        self.session_guard = SessionGuard(self.calendar, guards)
        self.lock = SafetyLock(settings.live_orders_env, settings.armed_file)
        self.halt = HaltSwitch(settings.halt_file)
        self.runner = Runner(engine_factory, interval=interval, clock=clock)
        self._macro, self._macro_error = self._load_macro()
        symbols = list(strategy.universe.symbols)
        self.feed = SnapshotFeed(market_data, symbols, refresh_seconds=refresh_seconds, clock=clock)
        self.started_at = clock()

    # ---------------------------------------------------------------- 준비
    def _load_macro(self):
        path = Path(self.guards.macro.calendar_file)
        if not path.is_absolute():
            path = self.settings.root / path
        try:
            return load_macro_calendar(path, self.guards.macro.drawdown_boost_pct), None
        except (MacroCalendarError, OSError) as exc:
            return None, str(exc)

    def start_feed(self) -> None:
        self.feed.start()

    def stop_feed(self) -> None:
        self.feed.stop()

    def prime(self) -> None:
        """테스트·즉시 확인용: 시세 스냅샷을 한 번 동기적으로 채운다."""
        self.feed.refresh_once()

    # ---------------------------------------------------------------- 상태
    @property
    def base_mode(self) -> str:
        return self.settings.normalized_mode

    def active_mode(self) -> str:
        return self.runner.state.mode or self.base_mode

    def live_block_reason(self) -> str | None:
        if not self.settings.live_orders_env:
            return "LIVE_ORDERS=true 가 아닙니다"
        if not self.lock.armed:
            return f"{self.settings.armed_file} 파일이 없습니다"
        if not self.settings.toss_client_id or not self.settings.toss_client_secret:
            return "TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 이 없습니다"
        return None

    def status(self) -> dict[str, Any]:
        now = self.clock()
        today = et_date(now)
        snapshot = self.feed.snapshot()
        repo = self.repo_factory()
        try:
            mode = self.active_mode()
            account = self._account_view(repo, snapshot, mode)
            equity = dec(account["equity_usd"]) if account.get("equity_usd") is not None else self.settings.paper_starting_cash
            cash = dec(account["cash_usd"]) if account.get("cash_usd") is not None else self.settings.paper_starting_cash
            capital = self.strategy.capital
            buffer = capital.cash_buffer(equity)
            spendable = max(cash - buffer, ZERO)
            positions = repo.positions()
            tier_entry, tier_cap = capital.tier(equity)
            day = repo.day_state(today)
            equity_open = day.equity_open if day is not None else None
            pnl_usd = (equity - equity_open) if equity_open is not None else ZERO
            pnl_pct = (pnl_usd / equity_open * Decimal(100)) if equity_open and equity_open > ZERO else ZERO
            unrealized = sum(
                (
                    position.qty * (self._last_price(snapshot, position.symbol) or position.avg_price)
                    - position.cost_basis
                    for position in positions
                ),
                ZERO,
            )
            session = self.session_guard.evaluate(now)
            guards_view = self._guards_view(snapshot, session, today, repo, equity)
            universe = self._universe_view(snapshot, now, positions, today)
            return {
                "ok": True,
                "version": __version__,
                "generated_at": now.isoformat(),
                "clock": {
                    "kst": _kst(now, "%Y-%m-%d %H:%M:%S KST"),
                    "et": _et(now, "%Y-%m-%d %H:%M ET"),
                    "et_short": _et(now, "%H:%M ET"),
                },
                "mode": mode,
                "base_mode": self.base_mode,
                "runner": self.runner.state.as_dict(self.runner.interval),
                "account": account,
                "today": {
                    "trade_date": today.isoformat(),
                    "equity_open": _money(equity_open),
                    "pnl_usd": _money(pnl_usd),
                    "pnl_pct": _s(pct4(pnl_pct)),
                    "realized_pnl": _money(day.realized_pnl) if day is not None else "0.00",
                    "unrealized_pnl": _money(unrealized),
                    "buys_halted_reason": None if day is None else day.buys_halted_reason,
                },
                "slots": {
                    "total": capital.slots,
                    "used": len(positions),
                    "free": free_slots(capital.slots, positions),
                    "tier": {"entry_usd": _money(tier_entry), "cap_usd": _money(tier_cap)},
                },
                "safety": self._safety_view(repo),
                "kill_switch": self._kill_switch_view(repo),
                "account_rules": self._account_rules_view(repo, today, equity),
                "session": self._session_view(session, now),
                "guards": guards_view,
                "universe": universe,
                "universe_note": self._universe_note(snapshot, now),
                "positions": [self._position_view(position, snapshot, today, repo) for position in positions],
                "orders": self._orders_view(repo),
                "alerts": self._alerts_view(),
            }
        finally:
            repo.close()

    def _account_view(self, repo: Repo, snapshot: MarketSnapshot, mode: str) -> dict[str, Any]:
        prices = {symbol: quote.last_price for symbol, quote in snapshot.quotes.items()}
        if mode == "paper":
            broker = PaperBroker(
                self.settings.paper_starting_cash,
                slippage_bps=self.strategy.paper.slippage_bps,
                commission_bps=self.strategy.paper.commission_bps,
            )
            stored = repo.kv_get_json("paper_state")
            if isinstance(stored, dict):
                try:
                    broker.restore(stored)
                except (TypeError, ValueError, KeyError):
                    LOG.warning("paper_state 스냅샷을 복원하지 못했습니다")
            broker.update_prices(prices)
            try:
                account = broker.account()
            except Exception as exc:  # noqa: BLE001
                return {"equity_usd": None, "cash_usd": None, "positions_value": None, "buffer_usd": None,
                        "spendable_usd": None, "source": "paper", "error": str(exc)}
            return self._account_payload(account, "paper")
        if mode == "live":
            broker = self.runner.broker("live")
            if broker is None:
                return {"equity_usd": None, "cash_usd": None, "positions_value": None, "buffer_usd": None,
                        "spendable_usd": None, "source": "live",
                        "note": "실계좌 잔고는 봇이 live 로 실행 중일 때만 표시됩니다"}
            try:
                account = broker.account()
            except Exception as exc:  # noqa: BLE001
                return {"equity_usd": None, "cash_usd": None, "positions_value": None, "buffer_usd": None,
                        "spendable_usd": None, "source": "live", "error": str(exc)}
            return self._account_payload(account, "live")
        broker = DryRunBroker(self.settings.paper_starting_cash, clock=self.clock)
        account = broker.account()
        return self._account_payload(account, "dry-run")

    def _account_payload(self, account, source: str) -> dict[str, Any]:
        equity = account.equity_usd
        cash = account.cash_usd
        buffer = self.strategy.capital.cash_buffer(equity)
        spendable = max(cash - buffer, ZERO)
        return {
            "equity_usd": _money(equity),
            "cash_usd": _money(cash),
            "positions_value": _money(account.exposure()),
            "buffer_usd": _money(buffer),
            "spendable_usd": _money(spendable),
            "source": source,
            "as_of": _kst(account.as_of) if account.as_of is not None else None,
        }

    def _safety_view(self, repo: Repo) -> dict[str, Any]:
        lock = self.lock.describe()
        state = repo.kv_get_json("reconcile_state")
        reconcile = state if isinstance(state, dict) else {}
        return {
            "halted": self.halt.halted,
            "halt_reason": self.halt.reason() if self.halt.halted else "",
            "live_orders_env": lock["live_orders_env"],
            "armed": lock["armed_file"],
            "live_allowed": lock["live_allowed"],
            "live_block_reason": self.live_block_reason() or "",
            "reconcile": {
                "date": reconcile.get("date"),
                "ok": reconcile.get("ok", None),
                "mismatches": list(reconcile.get("mismatches") or [])[:6],
            },
            "account_kill_switch": self._kill_switch_view(repo),
        }

    def _kill_switch_view(self, repo: Repo) -> dict[str, Any]:
        view = AccountKillSwitch(repo, self.guards.kill_switch).describe()
        return {
            "active": view["active"],
            "reason": view["reason"],
            "at": _kst(datetime.fromisoformat(str(view["at"]))) if view.get("at") else None,
            "detail": view["detail"],
            "api_error_streak": view["api_error_streak"],
            "peak_equity": _money(dec(view["peak_equity"])) if view.get("peak_equity") else None,
            "mdd_pct": _num(dec(view["mdd_pct"]), 2) if view.get("mdd_pct") else None,
        }

    def _account_rules_view(self, repo: Repo, today: date, equity: Decimal) -> dict[str, Any]:
        guard = AccountRegulationGuard(repo, self.guards.account, self.calendar)
        info = guard.describe(today, equity)
        pdt_block = bool(info["pdt_applies"]) and int(info["day_trades"]) >= int(info["day_trade_limit"])
        status = "block" if pdt_block else ("armed" if info["settlement"] == "cash" else "pass")
        if info["settlement"] == "cash":
            detail = f"현금 계좌 · 미결제 매도대금 ${info['unsettled_usd']} 는 T+1 까지 재매수 금지(GFV 방지)"
        else:
            detail = (
                f"마진 계좌 · 5영업일 내 데이트레이드 {info['day_trades']}/{info['day_trade_limit']}"
                + (" (자산 $25,000 미만 PDT 적용)" if info["pdt_applies"] else " (PDT 미적용)")
            )
        return {
            "status": status,
            "settlement": info["settlement"],
            "unsettled_usd": _money(dec(info["unsettled_usd"])),
            "unsettled_symbols": info["unsettled_symbols"],
            "day_trades": info["day_trades"],
            "day_trade_limit": info["day_trade_limit"],
            "pdt_applies": info["pdt_applies"],
            "label": ("PDT 한도 도달" if pdt_block else ("GFV 방지" if info["settlement"] == "cash" else "PDT 감시")),
            "detail": detail,
        }

    def _session_view(self, session, now: datetime) -> dict[str, Any]:
        window = session.window
        label = SESSION_LABELS.get(session.reason, session.reason or "정규장")
        return {
            "label": label,
            "is_open": session.is_open,
            "entry_allowed": session.entry_allowed,
            "fractional_sell_allowed": session.fractional_sell_allowed,
            "reason": session.reason,
            "trading_date": window.trading_date.isoformat() if window is not None else None,
            "open_et": window.regular_start.strftime("%H:%M") if window is not None else "09:30",
            "close_et": window.regular_end.strftime("%H:%M") if window is not None else "16:00",
            "early_close": bool(window.early_close) if window is not None else False,
            "source": window.source if window is not None else "static",
            "now_et": _et(now, "%H:%M"),
            "minutes_to_close": window.minutes_to_close(now) if window is not None and session.is_open else None,
        }

    def _guards_view(
        self,
        snapshot: MarketSnapshot,
        session,
        today: date,
        repo: Repo | None = None,
        equity: Decimal | None = None,
    ) -> dict[str, Any]:
        market_cfg = self.guards.market
        nq = snapshot.nq_change_pct
        vix = snapshot.vix
        if nq is None and vix is None:
            market_status = "unknown"
        elif nq is None or vix is None:
            market_status = "unknown" if market_cfg.fail_closed else "pass"
        elif nq <= -market_cfg.nq_block_pct:
            market_status = "block"
        elif vix >= market_cfg.vix_block:
            market_status = "block"
        else:
            market_status = "pass"
        blocked = list((self.runner.state.last_summary or {}).get("blocked") or [])

        def symbols_with(*needles: str) -> list[str]:
            out: set[str] = set()
            for item in blocked:
                symbol, _, reason = str(item).partition(":")
                if symbol and reason and any(needle in reason for needle in needles):
                    out.add(symbol)
            return sorted(out)[:6]

        macro_today = self._macro.events_on(today) if self._macro is not None else ()
        macro_next = self._next_macro(today)
        macro_status = "unknown" if self._macro is None and self.guards.macro.fail_closed else ("boost" if macro_today else "pass")
        data_view = self._data_guard_view(snapshot, today, session)
        session_label = SESSION_LABELS.get(session.reason, session.reason or "정규장")
        return {
            "market": {
                "status": market_status,
                "nq_symbol": market_cfg.nq_symbol,
                "nq_change_pct": _s(nq),
                "nq_block_pct": _s(market_cfg.nq_block_pct),
                "vix": _s(vix),
                "vix_block": _s(market_cfg.vix_block),
                "detail": "확인 불가 → 매수 차단(fail-closed)" if market_status == "unknown" else "NQ 선물·VIX 실시간 확인",
            },
            "session": {
                "status": "pass" if session.entry_allowed else "block",
                "label": session_label,
                "detail": (
                    f"매수 창 09:45~14:45 ET · 소수점 마감 15:00 ET"
                    + (" · 조기폐장 13:00" if session.window is not None and session.window.early_close else "")
                ),
            },
            "events": {
                "status": "block" if symbols_with("earnings") else "armed",
                "blocked": symbols_with("earnings"),
                "detail": f"실적 D-{self.guards.events.earnings_block_before_days}~당일 차단 · Nasdaq 캘린더 · 확인 불가 시 fail-closed",
            },
            "filings": {
                "status": "block" if symbols_with("8-k", "filing", "sec-") else "armed",
                "blocked": symbols_with("8-k", "filing", "sec-"),
                "detail": f"SEC 8-K 구조적 악재 키워드 {len(self.guards.filings.block_keywords)}개 · 최근 {self.guards.filings.lookback_days}일",
            },
            "news": {
                "status": "unknown" if not self.guards.news.enabled else ("block" if symbols_with("news") else "armed"),
                "enabled": self.guards.news.enabled,
                "blocked": symbols_with("news"),
                "detail": ("가드 꺼짐" if not self.guards.news.enabled
                           else f"Yahoo 헤드라인 BLOCK 키워드 {len(self.guards.news.block_keywords)}개"),
            },
            "macro": {
                "status": macro_status,
                "today_events": list(macro_today),
                "drawdown_boost_pct": _s(self.guards.macro.drawdown_boost_pct),
                "next": macro_next,
                "detail": ("거시 캘린더 파일을 읽지 못했습니다 → 매수 차단" if self._macro is None
                           else "FOMC·CPI·NFP 당일 요구 낙폭 " + str(self.guards.macro.drawdown_boost_pct) + "%p 강화"),
            },
            "data": data_view,
            "account": (
                self._account_rules_view(repo, today, equity if equity is not None else ZERO)
                if repo is not None
                else {"status": "unknown", "label": "계좌 정보 없음", "detail": ""}
            ),
            "kill_switch": (
                self._kill_switch_view(repo)
                if repo is not None
                else {"active": False, "reason": "", "detail": {}}
            ),
        }

    def _next_macro(self, today: date) -> str | None:
        if self._macro is None:
            return None
        upcoming = sorted(day for day in self._macro.dates() if day >= today)
        for day in upcoming[:1]:
            names = sorted(self._macro.events_on(day))
            return f"{day.isoformat()} {('/'.join(names))}"
        return None

    def _data_guard_view(self, snapshot: MarketSnapshot, today: date, session=None) -> dict[str, Any]:
        now = self.clock()
        stale: list[str] = []
        missing: list[str] = []
        for symbol in self.strategy.universe.symbols:
            quote = snapshot.quotes.get(symbol)
            if quote is None:
                missing.append(symbol)
                continue
            age = quote.age_seconds(now)
            if age is None or age > self.guards.data.max_quote_age_seconds:
                stale.append(symbol)
        session_open = bool(getattr(session, "is_open", True))
        if missing:
            label = "시세 없음: " + ", ".join(missing[:4]) + ("…" if len(missing) > 4 else "")
        elif stale and session_open:
            label = "시세 지연: " + ", ".join(stale[:4]) + ("…" if len(stale) > 4 else "")
        elif stale:
            label = "장 마감 · 마지막 시세 기준"
        else:
            label = "시세 정상"
        blocked = bool(missing) or (bool(stale) and session_open)
        return {
            "status": "block" if blocked else "pass",
            "label": label,
            "missing": missing,
            "stale": stale,
            "max_quote_age_seconds": self.guards.data.max_quote_age_seconds,
            "detail": f"시세 {self.guards.data.max_quote_age_seconds}초 이상 지연·결측 → 매수 보류 · 매도는 정상",
        }

    def _last_price(self, snapshot: MarketSnapshot, symbol: str) -> Decimal | None:
        quote = snapshot.quotes.get(symbol)
        return None if quote is None else quote.last_price

    def _universe_view(
        self,
        snapshot: MarketSnapshot,
        now: datetime,
        positions: Sequence[Position],
        today: date,
    ) -> list[dict[str, Any]]:
        held = {position.symbol for position in positions}
        entry_cfg = self.strategy.entry
        out: list[dict[str, Any]] = []
        for symbol in self.strategy.universe.symbols:
            quote = snapshot.quotes.get(symbol)
            daily = snapshot.daily.get(symbol) or []
            intraday = snapshot.intraday.get(symbol) or []
            closes = [bar.close for bar in daily]
            rsi2 = wilder_rsi(closes, entry_cfg.rsi_period) if closes else None
            atr = atr_pct(daily, entry_cfg.atr_period) if daily else None
            sma200 = sma(closes, entry_cfg.sma_period) if closes else None
            price = quote.last_price if quote is not None else None
            hod = robust_intraday_high(intraday, enabled=entry_cfg.hod_outlier_filter) if intraday else None
            if price is not None and (hod is None or hod < price):
                hod = price
            if price is None:
                out.append({
                    "symbol": symbol,
                    "weight": _s(self.strategy.universe.alpha(symbol)),
                    "last_price": None, "change_pct": None, "quote_age_seconds": None,
                    "hod": None, "drawdown_pct": None, "atr_pct": None, "threshold_pct": None,
                    "rsi2": None, "sma200": None, "sma200_gap_pct": None, "score": None,
                    "status": "nodata", "status_label": STATUS_REASONS["no-data"], "reason": "no-data",
                    "held": symbol in held,
                })
                continue
            inputs = SymbolInputs(
                symbol=symbol,
                last_price=price,
                hod=hod,
                rsi2=rsi2,
                atr_pct=atr,
                sma200=sma200,
                quote_time=None if quote is None else quote.timestamp,
            )
            evaluation = evaluate_entry(inputs, entry_cfg, self.strategy.universe)
            threshold, _fallback = entry_threshold_pct(inputs, entry_cfg)
            gap = None
            if sma200 is not None and sma200 > ZERO:
                gap = pct4((price - sma200) / sma200 * Decimal(100))
            if symbol in held:
                status, label, reason = "held", "보유 중", "held"
            elif evaluation.allowed:
                status, label, reason = "ready", STATUS_REASONS["ok"], "ok"
            elif rsi2 is None or sma200 is None:
                status, label, reason = "nodata", STATUS_REASONS[evaluation.reason] if evaluation.reason in STATUS_REASONS else "지표 결측", evaluation.reason
            else:
                status = "waiting"
                label = STATUS_REASONS.get(evaluation.reason, evaluation.reason)
                reason = evaluation.reason
            out.append({
                "symbol": symbol,
                "weight": _s(self.strategy.universe.alpha(symbol)),
                "last_price": _money(price),
                "change_pct": _num(snapshot.changes.get(symbol), 2),
                "quote_age_seconds": None if quote is None else quote.age_seconds(now),
                "hod": _money(hod),
                "drawdown_pct": _num(evaluation.drawdown_pct, 2),
                "atr_pct": _num(atr, 2),
                "threshold_pct": _num(threshold, 2),
                "rsi2": _num(rsi2, 1),
                "sma200": _money(sma200),
                "sma200_gap_pct": _num(gap, 1),
                "score": _num(evaluation.score, 3),
                "status": status,
                "status_label": label,
                "reason": reason,
                "held": symbol in held,
            })
        return out

    def _universe_note(self, snapshot: MarketSnapshot, now: datetime) -> str:
        stamp = _kst(snapshot.updated_at, "%H:%M:%S") or "–"
        if snapshot.fetching and not snapshot.complete:
            state = "시세 수집 중"
        elif snapshot.errors:
            state = f"일부 시세 실패 {len(snapshot.errors)}건"
        else:
            state = "시세 정상"
        vix = snapshot.vix
        if vix is None:
            tp_note = "VIX 미확인 → 진입 시 목표 익절 +5.0%(보수적)"
        else:
            target = self.strategy.exit.take_profit_low_vix_pct if vix < self.strategy.exit.take_profit_vix_low else self.strategy.exit.take_profit_high_vix_pct
            cutoff = self.strategy.exit.take_profit_vix_low
            tp_note = f"현재 VIX {_num(vix, 2)} → 신규 진입 목표 익절 +{_num(target, 1)}% (VIX<{_num(cutoff, 1)} 이면 +{_num(self.strategy.exit.take_profit_low_vix_pct, 1)}%)"
        return (
            f"기준 {stamp} KST · {state} · ATR(20)/RSI(2)/SMA(200)은 일봉 종가, HOD는 1분봉(중앙값×1.15 이상치 제거) "
            f"· 점수 = 알파 가중치 × min(|낙폭| ÷ 진입 임계, {self.strategy.entry.score_cap_multiple}) "
            f"· 고점 대비 -{self.strategy.entry.crash_atr_multiple}×ATR 초과 급락은 당일 제외 · {tp_note}"
        )

    def _position_view(
        self, position: Position, snapshot: MarketSnapshot, today: date, repo: Repo | None = None
    ) -> dict[str, Any]:
        last = self._last_price(snapshot, position.symbol) or position.avg_price
        pnl_pct = pct4((last - position.avg_price) / position.avg_price * Decimal(100)) if position.avg_price > ZERO else ZERO
        tp = position.avg_price * (Decimal(1) + position.tp_target_pct / Decimal(100))
        stop_pct = stop_loss_pct(position, self.strategy.exit)
        stop_price = position.avg_price * (Decimal(1) - stop_pct / Decimal(100))
        trading_days, calendar_days = hold_days(position, today, self.calendar)
        return {
            "symbol": position.symbol,
            "qty": _s(position.qty),
            "avg_price": _money(position.avg_price),
            "last_price": _money(last),
            "pnl_pct": _num(pnl_pct, 2),
            "pnl_usd": _money(position.qty * (last - position.avg_price)),
            "tp_target_pct": _num(position.tp_target_pct, 1),
            "tp_price": _money(tp),
            "stop_loss_pct": _num(stop_pct, 1),
            "stop_price": _money(stop_price),
            "add_count": position.add_count,
            "trading_days": trading_days,
            "calendar_days": calendar_days,
            "time_exit_trading_days": self.strategy.exit.time_exit_trading_days,
            "time_exit_calendar_days": self.strategy.exit.time_exit_calendar_days,
            "trailing": self._trail_view(repo, position.symbol) if repo is not None else None,
            "entry_at": _kst(position.entry_at),
            "first_entry_date": position.first_entry_date.isoformat(),
            "entry_vix": _s(position.entry_vix),
        }

    def _trail_view(self, repo: Repo, symbol: str) -> dict[str, Any] | None:
        value = repo.kv_get_json(f"trail:{symbol}")
        if not isinstance(value, dict) or not value.get("high"):
            return None
        return {
            "high": _money(dec(value["high"])),
            "stop_pct": _num(self.strategy.exit.trailing_drawdown_pct, 1),
        }

    def _orders_view(self, repo: Repo) -> list[dict[str, Any]]:
        try:
            records = repo.orders_since(datetime(1970, 1, 1, tzinfo=timezone.utc))[-25:]
        except Exception as exc:  # noqa: BLE001
            LOG.warning("주문 내역 조회 실패: %s", exc)
            return []
        out = []
        for record in reversed(records):
            out.append({
                "at_kst": _kst(record.created_at),
                "side": record.side,
                "symbol": record.symbol,
                "status": record.status,
                "qty": _s(record.filled_qty) if record.filled_qty and record.filled_qty > ZERO else (_s(record.qty) if record.qty else None),
                "avg_price": _money(record.avg_price),
                "amount": _money(record.amount_usd or record.filled_amount),
                "reason": record.reason,
                "mode": record.mode,
                "error": record.error_code,
            })
        return out

    def _alerts_view(self, limit: int = 15) -> list[dict[str, Any]]:
        path = self.settings.log_dir / "alerts.jsonl"
        if not path.is_file():
            return []
        try:
            with path.open("rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - 32_768))
                chunk = handle.read().decode("utf-8", errors="replace")
            lines = [line for line in chunk.splitlines() if line.strip()]
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for line in reversed(lines[-limit:]):
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            out.append({
                "at_kst": payload.get("kst") or payload.get("at"),
                "level": payload.get("level", "info"),
                "message": payload.get("message", ""),
            })
        return out

    # ---------------------------------------------------------------- 제어
    def control(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        action = str(payload.get("action") or "").strip()
        if action == "start":
            mode = str(payload.get("mode") or self.base_mode).strip()
            if mode not in ("dry-run", "paper", "live"):
                return 400, {"ok": False, "error": "invalid-mode", "detail": "mode 는 dry-run / paper / live 중 하나입니다"}
            if self.runner.running:
                return 409, {"ok": False, "error": "already-running", "detail": "이미 루프가 실행 중입니다"}
            if mode == "live":
                reason = self.live_block_reason()
                if reason:
                    return 403, {"ok": False, "error": "live-locked", "detail": f"실주문 잠금: {reason}"}
            try:
                self.runner.start(mode)
            except LiveOrdersDisabled as exc:
                return 403, {"ok": False, "error": "live-locked", "detail": str(exc)}
            except ConfigError as exc:
                return 400, {"ok": False, "error": "config-error", "detail": str(exc)}
            except Exception as exc:  # noqa: BLE001
                LOG.exception("웹에서 루프를 시작하지 못했습니다")
                return 500, {"ok": False, "error": "start-failed", "detail": str(exc)}
            return 200, {"ok": True, "message": f"{mode} 루프를 시작했습니다.", "runner": self.runner.state.as_dict(self.runner.interval)}
        if action == "stop":
            if not self.runner.running:
                return 200, {"ok": True, "message": "이미 정지 상태입니다.", "runner": self.runner.state.as_dict(self.runner.interval)}
            stopped = self.runner.stop()
            return 200, {
                "ok": True,
                "message": "루프를 정지했습니다." if stopped else "정지 신호를 보냈습니다(현재 사이클 종료 대기).",
                "runner": self.runner.state.as_dict(self.runner.interval),
            }
        if action in ("halt-on", "halt-off"):
            return self._set_halt(action == "halt-on")
        return 400, {"ok": False, "error": "unknown-action", "detail": "action 은 start / stop / halt-on / halt-off 입니다"}

    def _set_halt(self, enable: bool) -> tuple[int, dict[str, Any]]:
        path = Path(self.settings.halt_file)
        try:
            if enable:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("웹 대시보드 킬스위치 (신규 매수 정지, 청산은 계속)\n", encoding="utf-8")
            else:
                if path.exists():
                    path.unlink()
                # 수동 킬스위치를 해제하면 계좌 Kill Switch(Safe Halt)도 함께 해제한다.
                repo = self.repo_factory()
                try:
                    AccountKillSwitch(repo, self.guards.kill_switch).reset(self.clock())
                finally:
                    repo.close()
        except OSError as exc:
            return 500, {"ok": False, "error": "halt-write-failed", "detail": str(exc)}
        return 200, {
            "ok": True,
            "message": "킬스위치를 켰습니다. 신규 매수만 멈추고 청산은 계속합니다." if enable else "킬스위치와 계좌 Kill Switch 를 해제했습니다.",
            "halted": self.halt.halted,
        }

    def run_once(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        mode = str(payload.get("mode") or self.active_mode()).strip()
        if mode not in ("dry-run", "paper", "live"):
            return 400, {"ok": False, "error": "invalid-mode", "detail": "mode 는 dry-run / paper / live 중 하나입니다"}
        if mode == "live":
            reason = self.live_block_reason()
            if reason:
                return 403, {"ok": False, "error": "live-locked", "detail": f"실주문 잠금: {reason}"}
        try:
            summary = self.runner.run_cycle(mode)
        except LiveOrdersDisabled as exc:
            return 403, {"ok": False, "error": "live-locked", "detail": str(exc)}
        except ConfigError as exc:
            return 400, {"ok": False, "error": "config-error", "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001
            LOG.exception("웹 1회 실행 실패")
            return 500, {"ok": False, "error": "cycle-failed", "detail": str(exc)}
        return 200, {"ok": True, "message": "사이클 1회를 실행했습니다.", "report": dict(summary)}


# ====================================================================== HTTP 서버
MAX_BODY = 64 * 1024


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    app: WebApp = None  # type: ignore[assignment]
    server_version = f"AlphaShieldWeb/{__version__}"

    # -- 공통 ---------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib 시그니처
        LOG.debug("web %s - %s", self.address_string(), fmt % args)

    def _cors_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = urlparse(origin).hostname or ""
        return host in ("127.0.0.1", "localhost", "::1")

    def _token_ok(self) -> bool:
        supplied = self.headers.get("X-Alpha-Token") or ""
        if not supplied:
            query = urlparse(self.path).query
            for part in query.split("&"):
                key, _, value = part.partition("=")
                if key == "token":
                    supplied = value
        return secrets.compare_digest(str(supplied), self.app.token)

    def _json(self, code: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Mapping[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length < 0 or length > MAX_BODY:
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return None
        return payload if isinstance(payload, Mapping) else None

    # -- 라우트 -------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - stdlib 시그니처
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._html(PAGE.replace("__TOKEN__", self.app.token))
            return
        if path == "/api/health":
            self._json(200, {"ok": True, "version": __version__})
            return
        if path == "/api/status":
            if not self._token_ok():
                self._json(401, {"ok": False, "error": "unauthorized", "detail": "X-Alpha-Token 헤더가 필요합니다"})
                return
            try:
                self._json(200, self.app.status())
            except Exception as exc:  # noqa: BLE001
                LOG.exception("상태 조회 실패")
                self._json(500, {"ok": False, "error": "status-failed", "detail": str(exc)})
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._json(404, {"ok": False, "error": "not-found", "path": path})

    def do_POST(self) -> None:  # noqa: N802 - stdlib 시그니처
        path = urlparse(self.path).path
        if path not in ("/api/control", "/api/run-once"):
            self._json(404, {"ok": False, "error": "not-found", "path": path})
            return
        if not self._cors_ok():
            self._json(403, {"ok": False, "error": "bad-origin", "detail": "로컬 화면에서만 조작할 수 있습니다"})
            return
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type not in ("application/json", ""):
            self._json(415, {"ok": False, "error": "unsupported-media-type", "detail": "application/json 로 보내세요"})
            return
        if not self._token_ok():
            self._json(401, {"ok": False, "error": "unauthorized", "detail": "X-Alpha-Token 헤더가 필요합니다"})
            return
        payload = self._read_json()
        if payload is None:
            self._json(400, {"ok": False, "error": "bad-request", "detail": "JSON 본문을 읽을 수 없습니다"})
            return
        try:
            if path == "/api/control":
                code, body = self.app.control(payload)
            else:
                code, body = self.app.run_once(payload)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("웹 요청 실패 %s", path)
            code, body = 500, {"ok": False, "error": "internal", "detail": str(exc)}
        self._json(code, body)


def create_server(app: WebApp, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    handler = type("AlphaShieldHandler", (_Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.app = app  # type: ignore[attr-defined]
    return server


def serve(app: WebApp, host: str = "127.0.0.1", port: int = 8080) -> None:
    """블로킹 서버. Ctrl+C 로 종료한다."""
    app.start_feed()
    server = create_server(app, host, port)
    bound_host, bound_port = server.server_address[:2]
    LOG.info("웹 대시보드: http://%s:%s (로컬 전용)", bound_host, bound_port)
    print(f"ALPHA SHIELD 웹 대시보드: http://{bound_host}:{bound_port}")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("웹 대시보드를 종료합니다.")
    finally:
        server.shutdown()
        server.server_close()
        app.runner.stop()
        app.stop_feed()
