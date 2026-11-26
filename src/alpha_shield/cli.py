"""프로세스 실행 진입점 — dry-run / paper / live."""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .broker import DryRunBroker, PaperBroker, TossBroker
from .broker.paper import RepoPaperStore
from .calendar import StaticUsCalendar, TossUsCalendar
from .clock import SystemClock
from .config import ConfigError, Settings, load_all
from .engine.alerts import Alerter, DecisionLog
from .engine.loop import Engine
from .marketdata import (
    CompositeMarketData,
    MacroCalendarError,
    NasdaqEarningsCalendar,
    SecFilingsProvider,
    TossClient,
    TossMarketData,
    YahooHeadlineProvider,
    YahooMarketData,
    load_macro_calendar,
)
from .net import RetryingTransport, UrllibTransport
from .reporting.ledger import export_ledger
from .safety import LiveOrdersDisabled, SafetyLock
from .state.db import Database
from .state.repo import Repo

LOG = logging.getLogger("alpha_shield")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alpha-shield",
        description="ALPHA SHIELD v3.2 (6-1-F) 미국장 2슬롯 소수점 자동매매",
    )
    parser.add_argument("--version", action="version", version=f"alpha-shield {__version__}")
    parser.add_argument("--mode", choices=["dry-run", "paper", "live"], help="실행 모드 (기본: .env 의 ALPHA_MODE 또는 dry-run)")
    parser.add_argument("--config-root", default=None, help="프로젝트 루트 (기본: 자동 탐지)")
    parser.add_argument("--data-dir", default=None, help="상태 디렉터리 (기본: ./state)")
    parser.add_argument("--once", action="store_true", help="사이클 1회 후 종료")
    parser.add_argument("--loop", action="store_true", help="5분 주기로 계속 실행")
    parser.add_argument("--interval", type=int, default=300, help="사이클 간격(초, 기본 300)")
    parser.add_argument("--cycles", type=int, default=None, help="--loop 에서 최대 사이클 수")
    parser.add_argument("--check", action="store_true", help="설정·환경 점검만 하고 종료")
    parser.add_argument("--status", action="store_true", help="현재 포지션·주문·일일 상태 출력")
    parser.add_argument("--export-ledger", metavar="YYYY-MM", nargs="?", const="", help="거래 원장 CSV 내보내기")
    parser.add_argument("--reconcile", action="store_true", help="브로커 보유와 로컬 원장을 즉시 대조")
    return parser


def setup_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level, logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    if root.handlers:
        return
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(console)
    try:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            settings.log_dir / "alpha-shield.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(file_handler)
    except OSError as exc:  # pragma: no cover
        LOG.warning("파일 로그를 열 수 없습니다: %s", exc)


def build_engine(settings: Settings, strategy, guards, repo: Repo, root: Path) -> Engine:
    transport = RetryingTransport(UrllibTransport())
    yahoo = YahooMarketData(transport)
    clock = SystemClock()

    toss_client = None
    if settings.normalized_mode == "live":
        if not settings.toss_client_id or not settings.toss_client_secret:
            raise ConfigError("live 모드에는 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 이 필요합니다")
        toss_client = TossClient(
            settings.toss_client_id,
            settings.toss_client_secret,
            settings.token_path,
            base_url=settings.toss_base_url,
            account_seq=settings.toss_account_seq,
            transport=transport,
        )
        calendar = TossUsCalendar(toss_client, StaticUsCalendar(root / "config" / "market_calendar_2026.json"))
        market_data = CompositeMarketData(TossMarketData(toss_client), yahoo)
        safety = SafetyLock(settings.live_orders_env, settings.armed_file)
        safety.assert_live()
        broker = TossBroker(
            toss_client,
            safety,
            min_order_usd=strategy.capital.min_order_usd,
            session_provider=lambda now: calendar.session_on(now.date()),
            fractional_close_buffer_min=guards.session.fractional_close_buffer_min,
            clock=clock.now,
        )
    else:
        calendar = StaticUsCalendar(root / "config" / "market_calendar_2026.json")
        market_data = yahoo
        if settings.normalized_mode == "paper":
            broker = PaperBroker(
                settings.paper_starting_cash,
                slippage_bps=strategy.paper.slippage_bps,
                commission_bps=strategy.paper.commission_bps,
                store=RepoPaperStore(repo),
                clock=clock.now,
            )
        else:
            broker = DryRunBroker(settings.paper_starting_cash, clock=clock.now)

    earnings = NasdaqEarningsCalendar(transport, cache_seconds=guards.events.earnings_cache_seconds)
    filings = SecFilingsProvider(transport, settings.sec_user_agent)
    news = YahooHeadlineProvider(
        transport,
        guards.news.feed_url,
        limit=guards.news.lookback_items,
    )
    macro_path = Path(guards.macro.calendar_file)
    if not macro_path.is_absolute():
        macro_path = root / macro_path
    try:
        macro_calendar = load_macro_calendar(macro_path, guards.macro.drawdown_boost_pct)
    except MacroCalendarError as exc:
        LOG.error("%s — 거시 가드 fail-closed 로 동작합니다", exc)
        macro_calendar = None

    alerter = Alerter(
        settings.log_dir / "alerts.jsonl",
        webhook_url=settings.alert_webhook_url,
        transport=transport,
        clock=clock.now,
    )
    decision_log = DecisionLog(settings.log_dir / f"decisions-{clock.now().date().isoformat()}.jsonl")
    return Engine(
        mode=settings.normalized_mode,
        strategy=strategy,
        guards_config=guards,
        repo=repo,
        broker=broker,
        market_data=market_data,
        calendar=calendar,
        clock=clock,
        halt_file=settings.halt_file,
        alerter=alerter,
        decision_log=decision_log,
        earnings_provider=earnings,
        filings_provider=filings,
        news_provider=news,
        macro_calendar=macro_calendar,
    )


def print_check(settings: Settings, strategy, guards) -> None:
    print("=== ALPHA SHIELD v3.2 점검 ===")
    print(json.dumps(settings.as_safe_dict(), ensure_ascii=False, indent=2))
    print(f"유니버스: {', '.join(f'{symbol}({weight})' for symbol, weight in strategy.universe.weights.items())}")
    print(
        f"자금: 2슬롯 / <${strategy.capital.threshold_usd} ${strategy.capital.small_entry_usd}"
        f"·상한 ${strategy.capital.small_symbol_cap_usd} /"
        f">=${strategy.capital.threshold_usd} ${strategy.capital.large_entry_usd}"
        f"·상한 ${strategy.capital.large_symbol_cap_usd}"
    )
    print(
        f"버퍼: max(자산x{strategy.capital.cash_buffer_pct:.0%}, ${strategy.capital.cash_buffer_min_usd})"
        f" / 최소주문 ${strategy.capital.min_order_usd}"
    )
    print(
        f"진입: HOD -{strategy.entry.atr_drawdown_multiple}xATR({strategy.entry.atr_period})%, "
        f"RSI({strategy.entry.rsi_period})<={strategy.entry.rsi_max}, SMA{strategy.entry.sma_period}x"
        f"{strategy.entry.sma_floor_ratio}"
    )
    print(
        f"청산: 손절 -{strategy.exit.stop_loss_pct}% / 물타기 -{strategy.exit.stop_loss_added_pct}% / "
        f"익절 VIX<{strategy.exit.take_profit_vix_low} +{strategy.exit.take_profit_low_vix_pct}% "
        f"아니면 +{strategy.exit.take_profit_high_vix_pct}% / {strategy.exit.time_exit_trading_days}거래일"
    )
    print(f"가드: NQ -{guards.market.nq_block_pct}% / VIX {guards.market.vix_block} / 시세지연 {guards.data.max_quote_age_seconds}s")
    lock = SafetyLock(settings.live_orders_env, settings.armed_file)
    print(f"이중 안전잠금: {json.dumps(lock.describe(), ensure_ascii=False)}")
    if settings.normalized_mode == "live" and not lock.live_allowed:
        print("!! live 모드지만 이중 잠금이 풀리지 않았습니다 — 실주문이 거부됩니다.")


def print_status(repo: Repo) -> None:
    positions = repo.positions()
    print(f"포지션 {len(positions)}건")
    for position in positions:
        print(
            f"  {position.symbol}: qty={position.qty} avg={position.avg_price} "
            f"TP={position.tp_target_pct}% adds={position.add_count} entry={position.first_entry_date}"
        )
    print("최근 주문 10건")
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    recent = repo.orders_since(epoch)[-10:]
    for record in recent:
        print(
            f"  {record.created_at.isoformat()} {record.side:4} {record.symbol:6} "
            f"{record.status:14} qty={record.filled_qty} avg={record.avg_price} {record.reason}"
        )
    print(f"테이블: {repo.table_counts()}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = Path(args.config_root).resolve() if args.config_root else None
    try:
        settings, strategy, guards = load_all(root=root, mode=args.mode, data_dir=args.data_dir)
        setup_logging(settings)
    except ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2

    repo = Repo(Database(settings.db_path))
    try:
        if args.check:
            print_check(settings, strategy, guards)
            return 0
        if args.status:
            print_status(repo)
            return 0
        if args.export_ledger is not None:
            month = args.export_ledger or None
            path = export_ledger(repo, settings.root / "ledger", month)
            print(f"원장 내보내기 완료: {path}")
            return 0

        engine = build_engine(settings, strategy, guards, repo, settings.root)
        if args.reconcile:
            engine.bootstrap()
            state = repo.kv_get_json("reconcile_state")
            print(json.dumps(state, ensure_ascii=False, indent=2))
            return 0
        engine.bootstrap()
        if args.loop:
            engine.run_forever(interval_seconds=args.interval, max_cycles=args.cycles)
            return 0
        report = engine.run_cycle()
        print(
            json.dumps(
                {
                    "at": report.at.isoformat(),
                    "skipped": report.skipped,
                    "equity_usd": str(report.equity_usd),
                    "cash_usd": str(report.cash_usd),
                    "sells": [result.status for result in report.sells],
                    "buys": [result.status for result in report.buys],
                    "blocked": list(report.blocked),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except LiveOrdersDisabled as exc:
        print(f"실주문 잠금: {exc}", file=sys.stderr)
        return 3
    except ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("중단되었습니다.")
        return 130
    finally:
        repo.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
