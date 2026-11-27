import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from helpers import (
    EmptyEarnings,
    EmptyNews,
    FixedEarnings,
    MutableMarketData,
    NoFilings,
    ROOT,
    d,
    et,
    falling_closes,
    make_daily,
    make_intraday,
)

from alpha_shield.broker import DryRunBroker, PaperBroker
from alpha_shield.calendar import StaticUsCalendar
from alpha_shield.clock import FixedClock
from alpha_shield.config import load_guards, load_strategy
from alpha_shield.engine.alerts import Alerter, DecisionLog
from alpha_shield.engine.loop import Engine
from alpha_shield.marketdata.macro import MacroCalendar
from alpha_shield.models import ORDER_STATUS_DRY_RUN, ORDER_STATUS_FILLED, Position
from alpha_shield.numeric import ZERO
from alpha_shield.state.db import Database
from alpha_shield.state.repo import Repo


class Harness:
    def __init__(self, mode="paper", cash="380", now=None, vix="15.5", earnings=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.now = now or et(2026, 12, 1, 10, 30)
        self.clock = FixedClock(self.now)
        self.repo = Repo(Database(self.base / "state.db"))
        self.strategy = load_strategy(ROOT / "config" / "strategy.yaml")
        self.guards = load_guards(ROOT / "config" / "guards.yaml")
        self.calendar = StaticUsCalendar(ROOT / "config" / "market_calendar_2026.json")
        self.md = MutableMarketData()
        self.md.set_vix(d(vix))
        self.broker = (
            PaperBroker(d(cash), slippage_bps=d("0"), commission_bps=d("0"), clock=self.clock.now)
            if mode == "paper"
            else DryRunBroker(d(cash), clock=self.clock.now)
        )
        self.halt_file = self.base / "halt.flag"
        self.engine = Engine(
            mode=mode,
            strategy=self.strategy,
            guards_config=self.guards,
            repo=self.repo,
            broker=self.broker,
            market_data=self.md,
            calendar=self.calendar,
            clock=self.clock,
            halt_file=self.halt_file,
            alerter=Alerter(self.base / "alerts.jsonl", clock=self.clock.now),
            decision_log=DecisionLog(self.base / "decisions.jsonl"),
            earnings_provider=earnings or EmptyEarnings(),
            filings_provider=NoFilings(),
            news_provider=EmptyNews(),
            macro_calendar=MacroCalendar(ROOT / "config" / "macro_calendar_2026.json", d("0.5")),
        )
        self._prime_market()

    def _prime_market(self):
        day = self.now.date()
        self.md.set_daily("NVDA", make_daily(falling_closes(), last_day=date(2026, 11, 30)))
        self.md.set_intraday("NVDA", make_intraday([100.0, 100.5], 96, day=day))
        self.md.set_quote("NVDA", 96, at=self.now - timedelta(seconds=10))

    def cycle(self):
        return self.engine.run_cycle()

    def close(self):
        self.repo.close()
        self.tmp.cleanup()

    def orders(self):
        epoch = datetime(1970, 1, 1, tzinfo=self.now.tzinfo)
        return self.repo.orders_since(epoch)


class EnginePaperTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_entry_creates_position_and_ledger(self):
        report = self.h.cycle()
        self.assertEqual(len(report.buys), 1)
        self.assertEqual(report.buys[0].status, ORDER_STATUS_FILLED)
        position = self.h.repo.position("NVDA")
        self.assertIsNotNone(position)
        self.assertEqual(position.qty, d("0.781250"))  # 75 / 96 (v3.3 $400 미만 구간), 소수 6자리 내림
        self.assertEqual(position.avg_price, d("96"))
        self.assertEqual(position.tp_target_pct, d("8.0"))  # VIX 15.5 < 18
        self.assertEqual(position.entry_vix, d("15.5"))
        self.assertEqual(self.h.broker.account().cash_usd.quantize(d("0.01")), d("305.00"))
        records = self.h.orders()
        self.assertEqual(records[0].side, "BUY")
        self.assertEqual(records[0].status, "FILLED")

    def test_second_slot_entry_selects_higher_score(self):
        day = self.h.now.date()
        # TSLA: 더 깊은 낙폭(-6%) -> 점수 우위, NVDA 는 -4.5%
        self.h.md.set_daily("TSLA", make_daily(falling_closes(), last_day=date(2026, 11, 30)))
        self.h.md.set_intraday("TSLA", make_intraday([100.0], 96, day=day))
        self.h.md.set_quote("TSLA", 96, at=self.h.now - timedelta(seconds=10))
        report = self.h.cycle()
        self.assertEqual(len([r for r in report.buys if r.status == ORDER_STATUS_FILLED]), 2)
        self.assertEqual(self.h.repo.position("TSLA").symbol, "TSLA")
        self.assertEqual(self.h.repo.position("NVDA").symbol, "NVDA")

    def test_take_profit_sells_and_sets_cooldown(self):
        self.h.cycle()
        self.h.clock.advance(300)
        self.h.md.set_quote("NVDA", 103.68, at=self.h.clock.now() - timedelta(seconds=10))  # +8%
        report = self.h.cycle()
        self.assertEqual(len(report.sells), 1)
        self.assertEqual(report.sells[0].client_order_id.split("-")[3][0], "t")
        self.assertIsNone(self.h.repo.position("NVDA"))
        until = self.h.repo.cooldown_until("NVDA")
        self.assertIsNotNone(until)
        self.assertEqual(until, self.h.clock.now() + timedelta(seconds=900))

    def test_stop_loss_sells(self):
        self.h.cycle()
        self.h.clock.advance(300)
        self.h.md.set_quote("NVDA", 91.0, at=self.h.clock.now() - timedelta(seconds=10))  # -5.2%
        report = self.h.cycle()
        self.assertEqual(len(report.sells), 1)
        record = [r for r in self.h.orders() if r.side == "SELL"][0]
        self.assertEqual(record.reason, "stop_loss")

    def test_time_exit_without_quote_still_sells(self):
        self.h.repo.save_position(
            Position("NVDA", d("0.5"), d("100"), et(2026, 11, 16, 10), date(2026, 11, 16), d("8"))
        )
        self.h.broker._positions = {"NVDA": {"qty": d("0.5"), "avg_price": d("100")}}
        self.h.md._quotes.pop("NVDA", None)  # 시세 결측
        report = self.h.cycle()
        self.assertEqual(len(report.sells), 1)
        self.assertEqual(report.sells[0].status, ORDER_STATUS_FILLED)
        record = [r for r in self.h.orders() if r.side == "SELL"][0]
        self.assertEqual(record.reason, "time_exit_no_quote")

    def test_hold_between_stop_and_target(self):
        self.h.cycle()
        self.h.clock.advance(300)
        self.h.md.set_quote("NVDA", 101.0, at=self.h.clock.now() - timedelta(seconds=10))
        report = self.h.cycle()
        self.assertEqual(report.sells, ())
        self.assertIsNotNone(self.h.repo.position("NVDA"))

    def test_add_position_on_dip(self):
        self.h.cycle()
        position = self.h.repo.position("NVDA")
        self.assertEqual(position.add_count, 0)
        self.h.clock.advance(2 * 60 * 60)  # v3.3: 1차 진입 후 최소 2시간 경과
        # 평단 96 대비 -2% (94.08), RSI(2) 는 여전히 낮아야 한다
        closes = falling_closes()
        closes[-1] = d("94.08")
        closes[-2] = d("95.5")
        self.h.md.set_daily("NVDA", make_daily(closes, last_day=date(2026, 11, 30)))
        self.h.md.set_quote("NVDA", 94.08, at=self.h.clock.now() - timedelta(seconds=10))
        report = self.h.cycle()
        updated = self.h.repo.position("NVDA")
        self.assertEqual(updated.add_count, 1)
        self.assertGreater(updated.qty, position.qty)
        self.assertEqual(updated.tp_target_pct, d("8.0"))  # 물타기 후에도 유지

    def test_time_exit_after_ten_trading_days(self):
        self.h.repo.save_position(
            Position("NVDA", d("0.5"), d("100"), et(2026, 11, 16, 10), date(2026, 11, 16), d("8"))
        )
        self.h.broker.update_prices({"NVDA": d("100")})
        self.h.broker._positions = {"NVDA": {"qty": d("0.5"), "avg_price": d("100")}}
        self.h.md.set_quote("NVDA", 100, at=self.h.now - timedelta(seconds=10))
        report = self.h.cycle()
        self.assertTrue(any(result.status == ORDER_STATUS_FILLED for result in report.sells))
        record = [r for r in self.h.orders() if r.side == "SELL"][0]
        self.assertEqual(record.reason, "time_exit")

    def test_sell_priority_before_buy(self):
        # 기존 포지션은 손절, 동시에 신규 후보 존재 -> 매도가 먼저
        self.h.repo.save_position(
            Position("AAPL", d("0.5"), d("100"), et(2026, 12, 1, 10), date(2026, 12, 1), d("8"))
        )
        self.h.broker._positions = {"AAPL": {"qty": d("0.5"), "avg_price": d("100")}}
        self.h.md.set_daily("AAPL", make_daily([100] * 210, last_day=date(2026, 11, 30)))
        self.h.md.set_quote("AAPL", 90, at=self.h.now - timedelta(seconds=10))
        report = self.h.cycle()
        self.assertEqual(len(report.sells), 1)
        self.assertEqual(len(report.buys), 1)
        sides = [record.side for record in self.h.orders()]
        self.assertEqual(sides[0], "SELL")
        self.assertIn("BUY", sides)

    def test_vix_panic_blocks_buys(self):
        self.h.md.set_vix(d("30"))
        report = self.h.cycle()
        self.assertEqual(report.buys, ())
        self.assertIn("market:vix-panic", report.blocked)

    def test_nq_crash_blocks_buys(self):
        self.h.md.set_futures(d("-1.6"))
        report = self.h.cycle()
        self.assertEqual(report.buys, ())
        self.assertIn("market:nq-crash", report.blocked)

    def test_opening_grace_blocks_entry(self):
        self.h.clock.set(et(2026, 12, 1, 9, 40))
        report = self.h.cycle()
        self.assertEqual(report.buys, ())
        self.assertIn("session:opening-grace", report.blocked)

    def test_closing_grace_blocks_entry_but_allows_sell(self):
        self.h.repo.save_position(
            Position("NVDA", d("0.5"), d("100"), et(2026, 12, 1, 10), date(2026, 12, 1), d("8"))
        )
        self.h.broker._positions = {"NVDA": {"qty": d("0.5"), "avg_price": d("100")}}
        self.h.clock.set(et(2026, 12, 1, 14, 50))
        self.h.md.set_quote("NVDA", 90, at=self.h.clock.now() - timedelta(seconds=10))
        report = self.h.cycle()
        self.assertEqual(len(report.sells), 1)
        self.assertIn("session:closing-grace", report.blocked)

    def test_after_1500_cannot_sell_fractional(self):
        self.h.repo.save_position(
            Position("NVDA", d("0.5"), d("100"), et(2026, 12, 1, 10), date(2026, 12, 1), d("8"))
        )
        self.h.broker._positions = {"NVDA": {"qty": d("0.5"), "avg_price": d("100")}}
        self.h.clock.set(et(2026, 12, 1, 15, 5))
        self.h.md.set_quote("NVDA", 90, at=self.h.clock.now() - timedelta(seconds=10))
        report = self.h.cycle()
        self.assertEqual(report.sells, ())
        self.assertIn("NVDA:sell-window-closed", report.blocked)

    def test_stale_quote_blocks_symbol(self):
        self.h.md.set_quote("NVDA", 96, at=self.h.now - timedelta(minutes=16))
        report = self.h.cycle()
        self.assertEqual(report.buys, ())
        self.assertIn("NVDA:quote-stale", report.blocked)

    def test_earnings_today_blocks_symbol(self):
        self.h.engine.earnings_guard = type(self.h.engine.earnings_guard)(
            FixedEarnings({self.h.now.date(): {"NVDA"}}), self.h.guards.events, self.h.calendar
        )
        report = self.h.cycle()
        self.assertEqual(report.buys, ())
        self.assertIn("NVDA:earnings-today", report.blocked)

    def test_kill_switch_blocks_buys_but_allows_sell(self):
        self.h.halt_file.write_text("manual", encoding="utf-8")
        self.h.repo.save_position(
            Position("NVDA", d("0.5"), d("100"), et(2026, 12, 1, 10), date(2026, 12, 1), d("8"))
        )
        self.h.broker._positions = {"NVDA": {"qty": d("0.5"), "avg_price": d("100")}}
        self.h.md.set_quote("NVDA", 90, at=self.h.now - timedelta(seconds=10))
        report = self.h.cycle()
        self.assertEqual(len(report.sells), 1)
        self.assertEqual(report.buys, ())
        self.assertIn("kill-switch:manual", report.blocked)

    def test_reconcile_mismatch_blocks_buys(self):
        # 로컬에만 있는 포지션 (브로커 미보유) -> 불일치
        self.h.repo.save_position(
            Position("AAPL", d("0.5"), d("100"), et(2026, 12, 1, 10), date(2026, 12, 1), d("8"))
        )
        self.h.md.set_daily("AAPL", make_daily([100] * 210, last_day=date(2026, 11, 30)))
        self.h.md.set_quote("AAPL", 100, at=self.h.now - timedelta(seconds=10))
        report = self.h.cycle()
        self.assertEqual(report.buys, ())
        self.assertIn("reconcile-mismatch", report.blocked)
        state = self.h.repo.kv_get_json("reconcile_state")
        self.assertFalse(state["ok"])

    def test_bootstrap_reconciles_even_after_same_day_state(self):
        # 같은 날짜에 이미 '일치'로 기록되어 있어도 재시작(bootstrap)은 다시 대조한다
        self.h.repo.kv_set_json("reconcile_state", {"date": self.h.now.date().isoformat(), "ok": True})
        self.h.repo.save_position(
            Position("AAPL", d("0.5"), d("100"), et(2026, 12, 1, 10), date(2026, 12, 1), d("8"))
        )
        self.h.engine.bootstrap()
        state = self.h.repo.kv_get_json("reconcile_state")
        self.assertFalse(state["ok"])
        self.assertIn("AAPL", state["mismatches"][0])

    def test_daily_loss_limit_blocks_buys(self):
        self.h.repo.ensure_day_state(self.h.now.date(), d("400"), self.h.now)
        report = self.h.cycle()
        self.assertEqual(report.buys, ())
        self.assertIn("risk:daily-loss-limit", report.blocked)

    def test_signal_created_and_consumed(self):
        self.h.cycle()
        signals = self.h.repo.signals("CONSUMED")
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].symbol, "NVDA")

    def test_decision_log_written(self):
        self.h.cycle()
        path = self.h.base / "decisions.jsonl"
        self.assertTrue(path.exists())
        self.assertIn("NVDA", path.read_text(encoding="utf-8"))


class EngineDryRunTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(mode="dry-run")

    def tearDown(self):
        self.h.close()

    def test_dry_run_sends_nothing(self):
        report = self.h.cycle()
        self.assertEqual(len(report.buys), 1)
        self.assertEqual(report.buys[0].status, ORDER_STATUS_DRY_RUN)
        self.assertEqual(self.h.broker.intents[0].symbol, "NVDA")
        self.assertIsNone(self.h.repo.position("NVDA"))
        self.assertEqual(self.h.broker.account().cash_usd, d("380"))


class PendingBroker(PaperBroker):
    """첫 주문을 PENDING 으로 돌려주고, 다음 조회에서 체결된 것으로 보고한다."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = 0
        self.pending = None

    def place_order(self, request):
        self.calls += 1
        if self.calls == 1:
            from alpha_shield.models import OrderResult

            self.pending = request
            return OrderResult(client_order_id=request.client_order_id, status="PENDING", broker_order_id="P-1")
        return super().place_order(request)

    def get_order(self, broker_order_id):
        from alpha_shield.models import OrderResult

        if broker_order_id == "P-1" and self.pending is not None:
            return OrderResult(
                client_order_id=self.pending.client_order_id,
                status="FILLED",
                broker_order_id="P-1",
                filled_qty=d("0.85"),
                avg_price=d("100"),
                filled_amount=d("85"),
                commission=ZERO,
                filled_at=self._clock(),
            )
        return None


class EnginePendingSyncTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.h.broker = PendingBroker(d("380"), slippage_bps=d("0"), commission_bps=d("0"), clock=self.h.clock.now)
        self.h.engine.broker = self.h.broker

    def tearDown(self):
        self.h.close()

    def test_pending_buy_is_synced_next_cycle(self):
        report = self.h.cycle()
        self.assertEqual(report.buys[0].status, "PENDING")
        self.assertIsNone(self.h.repo.position("NVDA"))
        self.h.clock.advance(300)
        self.h.md.set_quote("NVDA", 100, at=self.h.clock.now() - timedelta(seconds=10))
        self.h.cycle()
        position = self.h.repo.position("NVDA")
        self.assertIsNotNone(position)
        self.assertEqual(position.qty, d("0.85"))
        self.assertEqual(position.avg_price, d("100"))
        record = self.h.repo.order_by_client_id(report.buys[0].client_order_id)
        self.assertEqual(record.status, "FILLED")


class EngineMarketClosedTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(now=et(2026, 11, 26, 11, 0))  # 추수감사절

    def tearDown(self):
        self.h.close()

    def test_holiday_skips(self):
        report = self.h.cycle()
        self.assertEqual(report.skipped, "market-holiday")
        self.assertEqual(report.buys, ())


if __name__ == "__main__":
    unittest.main()
