"""ALPHA SHIELD v3.3 (6-1-F) 가드·프로토콜 테스트.

PDF 개정 보고서의 전수 감사 스위트 TC-806 ~ TC-813 과 v3.3 신규 규칙
(3단 사이징 · 점수 2.5배 캡 · 비정상 급락 가드 · 계좌 규정 · 계좌 Kill Switch ·
이상 틱 가드 · 트레일링 · 프리마켓 예약 매도)을 고정한다.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from helpers import (
    EmptyEarnings,
    EmptyNews,
    MutableMarketData,
    NoFilings,
    ROOT,
    et,
    falling_closes,
    make_daily,
    make_intraday,
)

from alpha_shield.broker import PaperBroker
from alpha_shield.calendar import StaticUsCalendar
from alpha_shield.clock import FixedClock
from alpha_shield.config import load_guards, load_strategy
from alpha_shield.engine.alerts import Alerter, DecisionLog
from alpha_shield.engine.loop import Engine
from alpha_shield.guards.account import AccountRegulationGuard
from alpha_shield.guards.data import TickGuard
from alpha_shield.marketdata.macro import MacroCalendar
from alpha_shield.models import Bar, Position
from alpha_shield.numeric import ZERO, dec
from alpha_shield.state.db import Database
from alpha_shield.state.repo import Repo
from alpha_shield.strategy.entry import SymbolInputs, evaluate_entry
from alpha_shield.strategy.exit import evaluate_exit
from alpha_shield.strategy.signals import STATUS_DISCARDED, SignalBook


class Harness:
    """v3.3 엔진 하네스 — 자금·시각·보조 시세·프리마켓 플래그를 바꿀 수 있다."""

    def __init__(self, cash="380", now=None, vix="15.5", crosscheck=None, premarket=False):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.now = now or et(2026, 12, 1, 10, 30)
        self.clock = FixedClock(self.now)
        self.repo = Repo(Database(self.base / "state.db"))
        self.strategy = load_strategy(ROOT / "config" / "strategy.yaml")
        self.guards = load_guards(ROOT / "config" / "guards.yaml")
        self.calendar = StaticUsCalendar(ROOT / "config" / "market_calendar_2026.json")
        self.md = MutableMarketData()
        self.md.set_vix(dec(vix))
        self.broker = PaperBroker(dec(cash), slippage_bps=ZERO, commission_bps=ZERO, clock=self.clock.now)
        self.halt_file = self.base / "halt.flag"
        self.engine = Engine(
            mode="paper",
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
            earnings_provider=EmptyEarnings(),
            filings_provider=NoFilings(),
            news_provider=EmptyNews(),
            macro_calendar=MacroCalendar(ROOT / "config" / "macro_calendar_2026.json", dec("0.5")),
            allow_fractional_premarket_sell=premarket,
            tick_crosscheck=crosscheck,
        )
        self._prime_market()

    # ------------------------------------------------------------------ 준비
    def _prime_market(self) -> None:
        day = self.now.date()
        self.md.set_daily("NVDA", make_daily(falling_closes(), last_day=date(2026, 11, 30)))
        self.md.set_intraday("NVDA", make_intraday([100.0, 100.5], 96, day=day))
        self.md.set_quote("NVDA", 96, at=self.now - timedelta(seconds=10))

    def seed_position(
        self,
        symbol: str,
        qty: object,
        avg: object,
        *,
        price: object | None = None,
        add_count: int = 0,
        entry_at=None,
        first_entry_date: date | None = None,
        tp: object = "8",
    ) -> Position:
        entry_at = entry_at or (self.now - timedelta(hours=3))
        position = Position(
            symbol=symbol,
            qty=dec(qty),
            avg_price=dec(avg),
            entry_at=entry_at,
            first_entry_date=first_entry_date or self.now.date(),
            entry_vix=dec("15.5"),
            tp_target_pct=dec(tp),
            add_count=add_count,
        )
        self.repo.save_position(position)
        self.broker._positions[symbol] = {"qty": position.qty, "avg_price": position.avg_price}
        if price is not None:
            self.broker.update_prices({symbol: dec(price)})
        return position

    def daily(self, symbol: str, closes=None, spread="0.01", last_day: date | None = None):
        self.md.set_daily(
            symbol,
            make_daily(
                closes if closes is not None else falling_closes(),
                last_day=last_day or date(2026, 11, 30),
                spread=spread,
            ),
        )

    def quote(self, symbol: str, price: object, at=None):
        self.md.set_quote(symbol, price, at=at or (self.clock.now() - timedelta(seconds=10)))

    def cycle(self):
        return self.engine.run_cycle()

    def adds(self, report):
        out = []
        for result in report.buys:
            record = self.repo.order_by_client_id(result.client_order_id)
            if record is not None and record.reason == "add":
                out.append(result)
        return out

    def orders(self):
        epoch = self.now.replace(year=1970, month=1, day=1, hour=0, minute=0, second=0)
        return self.repo.orders_since(epoch)

    def alerts_text(self) -> str:
        path = self.base / "alerts.jsonl"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def close(self) -> None:
        self.repo.close()
        self.tmp.cleanup()


def near_intraday(price: object, now, minutes_before=(24, 20, 16, 12, 8)) -> list[Bar]:
    """now 직전 5분봉 기준가로 쓸 1분봉 — 최근 시각으로 만들어 '직전 5분봉' 판정이 되게 한다."""
    bars: list[Bar] = []
    for offset in minutes_before:
        ts = now - timedelta(minutes=offset)
        bars.append(Bar(timestamp=ts, open=dec(price), high=dec(price), low=dec(price), close=dec(price)))
    return bars


# ====================================================================== TC-806
class TC806StopLossEODCooldown(unittest.TestCase):
    """동일 종목 손절 후 15분 경과, 당일 진입 조건 재충족 → 당일 EOD 쿨다운으로 신규 매수 차단."""

    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_stop_loss_blocks_same_day_reentry(self):
        # 변동성 큰 종목(ATR ~6%) — 손절 폭이 비정상 급락 가드(3xATR)에 걸리지 않게 한다
        self.h.daily("NVDA", spread="0.03")
        self.h.cycle()  # 10:30 신규 진입
        self.assertIsNotNone(self.h.repo.position("NVDA"))

        self.h.clock.advance(300)  # 10:35
        self.h.quote("NVDA", 91)  # -5.2% → 손절
        report = self.h.cycle()
        self.assertEqual(len(report.sells), 1)
        self.assertIsNone(self.h.repo.position("NVDA"))

        # 쿨다운은 15분이 아니라 당일 마감(16:00 ET)까지
        until = self.h.repo.cooldown_until("NVDA")
        self.assertEqual(self.h.repo.cooldown_reason("NVDA"), "stop_loss_eod")
        self.assertEqual(until, et(2026, 12, 1, 16, 0))

        # 15분 뒤(10:55) 눌림 조건이 다시 충족되어도 당일은 재진입 금지
        self.h.clock.advance(20 * 60)
        self.h.quote("NVDA", 96)
        report = self.h.cycle()
        self.assertEqual(report.buys, ())
        self.assertIn("NVDA:cooldown-stop_loss_eod", report.blocked)
        self.assertIsNone(self.h.repo.position("NVDA"))

    def test_next_trading_day_reentry_allowed(self):
        self.h.cycle()
        self.h.clock.advance(300)
        self.h.quote("NVDA", 91)
        self.h.cycle()

        self.h.clock.set(et(2026, 12, 2, 10, 30))
        self.h.daily("NVDA", spread="0.03")
        self.h.quote("NVDA", 96)
        report = self.h.cycle()
        self.assertEqual(len([r for r in report.buys if r.status == "FILLED"]), 1)
        self.assertIsNotNone(self.h.repo.position("NVDA"))

    def test_take_profit_keeps_15_minute_cooldown(self):
        self.h.cycle()
        self.h.clock.advance(300)
        self.h.quote("NVDA", 103.68)  # +8%
        self.h.cycle()
        self.assertEqual(self.h.repo.cooldown_reason("NVDA"), "exit")
        self.assertEqual(self.h.repo.cooldown_until("NVDA"), self.h.clock.now() + timedelta(seconds=900))


# ====================================================================== TC-807
class TC807SizingBufferReconciliation(unittest.TestCase):
    """자산 $375, 1슬롯 $170 + 2슬롯 $85 보유 중 물타기 신호 → 버퍼 보존 · 부분 물타기 또는 $75/$150 준수."""

    def setUp(self):
        self.h = Harness(cash="120")

    def tearDown(self):
        self.h.close()

    def test_add_blocked_when_exposure_exceeds_new_cap(self):
        # 기존($85 + $85 = $170) 노출은 v3.3 상한 $150 을 넘는다 → 추가 매수 금지
        self.h.seed_position("NVDA", "1.7", "100", price="100")
        self.h.seed_position("AAPL", "0.85", "100", price="100")
        self.h.daily("NVDA")
        self.h.quote("NVDA", 98)  # -2.0% (ATR 기준 1.6% 충족)

        report = self.h.cycle()

        self.assertEqual(self.h.adds(report), [])
        self.assertIn("NVDA:add-cap-or-min-order", report.blocked)
        self.assertEqual(self.h.broker.account().cash_usd, dec("120"))  # 버퍼 $56.25 이상 보존

    def test_partial_add_limited_by_buffer(self):
        # 노출 $78.4 + $176.6 = $255, 현금 $120, 총자산 $375
        self.h.seed_position("NVDA", "0.8", "100", price="100")
        self.h.seed_position("AAPL", "1.766", "100", price="100")
        self.h.daily("NVDA")
        self.h.quote("NVDA", 98)

        report = self.h.cycle()

        adds = self.h.adds(report)
        self.assertEqual(len(adds), 1)
        # 1회 진입 $75 · 상한 여유 $71.6 · 버퍼 차감 가용현금 $63.75 → $63.75
        self.assertEqual(self.h.repo.order_by_client_id(adds[0].client_order_id).amount_usd, dec("63.75"))
        # 주문 뒤에도 현금은 안전 버퍼($56.25)와 같거나 많다
        self.assertGreaterEqual(self.h.broker.account().cash_usd, self.h.strategy.capital.cash_buffer(dec("375")))
        self.assertGreaterEqual(self.h.repo.position("NVDA").add_count, 1)


# ====================================================================== TC-808
class TC808AddIntervalAndATR(unittest.TestCase):
    """1차 진입 30분 만에 -1.6% 하락 + RSI(2) <= 35 → 최소 대기 2시간·0.8xATR 미달로 물타기 보류."""

    def setUp(self):
        self.h = Harness(cash="380")

    def tearDown(self):
        self.h.close()

    def test_interval_blocks_early_add(self):
        self.h.seed_position("NVDA", "0.8", "100", price="100", entry_at=self.h.now - timedelta(minutes=30))
        self.h.daily("NVDA")
        self.h.quote("NVDA", 98.0)  # -2.0% (ATR 기준 충족, 2시간은 미달)

        report = self.h.cycle()

        self.assertEqual(self.h.adds(report), [])
        self.assertIn("NVDA:add-add-interval", report.blocked)

    def test_atr_multiple_raises_the_bar(self):
        # ATR 3% -> 요구 하락 2.4%. -1.6% 는 부족하다.
        self.h.seed_position("NVDA", "0.8", "100", price="100", entry_at=self.h.now - timedelta(hours=3))
        self.h.daily("NVDA", spread="0.015")
        self.h.quote("NVDA", 98.4)

        report = self.h.cycle()

        self.assertEqual(self.h.adds(report), [])
        self.assertIn("NVDA:add-no-add-dip", report.blocked)


# ====================================================================== TC-809
class TC809AbnormalCrashGuard(unittest.TestCase):
    """장중 돌발 악재로 TSLA -10% 폭락 → 비정상 급락 가드 및 Hark 2 2.5배 상한 캡으로 매수 차단."""

    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_crash_symbol_excluded_for_the_day(self):
        day = self.h.now.date()
        self.h.daily("TSLA")
        self.h.md.set_intraday("TSLA", make_intraday([100.0], 90, day=day))
        self.h.quote("TSLA", 90)  # 고점 대비 -10%, ATR(20) ~2% → -6% 초과

        report = self.h.cycle()

        self.assertEqual([r for r in report.buys if "TSLA" in r.client_order_id], [])
        self.assertIn("TSLA:abnormal-crash", report.blocked)
        self.assertIn("TSLA", self.h.engine._crash_guard_symbols(day))

        # 반등해도 당일은 신규 매수 대상에서 제외된다
        self.h.clock.advance(300)
        self.h.quote("TSLA", 96.5)
        report = self.h.cycle()
        self.assertIn("TSLA:abnormal-crash", report.blocked)
        self.assertIsNone(self.h.repo.position("TSLA"))

    def test_score_is_capped_at_25(self):
        strategy = self.h.strategy
        inputs = SymbolInputs(
            symbol="NVDA", last_price=dec("80"), hod=dec("100"), rsi2=dec("5"),
            atr_pct=dec("2.0"), sma200=dec("85"),
        )
        evaluation = evaluate_entry(inputs, strategy.entry, strategy.universe)
        # |낙폭|/임계 = 20/1.04 = 19.2 → 2.5배 캡 → 1.5 x 2.5
        self.assertEqual(evaluation.score, dec("3.7500"))

    def test_hark2_score_uncapped_for_moderate_dip(self):
        strategy = self.h.strategy
        inputs = SymbolInputs(
            symbol="NVDA", last_price=dec("98"), hod=dec("100"), rsi2=dec("9"),
            atr_pct=dec("2.0"), sma200=dec("90"),
        )
        evaluation = evaluate_entry(inputs, strategy.entry, strategy.universe)
        self.assertEqual(evaluation.threshold_pct, dec("1.04"))
        self.assertEqual(evaluation.score, dec("2.8846"))  # 1.5 x (2 / 1.04)


# ====================================================================== TC-810
class TC810TickGuard(unittest.TestCase):
    """시세 오류로 직전 5분봉 대비 -15% 단발 스파이크 수신 → 보조 소스 불일치 시 가짜 손절 차단 + 알림."""

    def setUp(self):
        self.h = Harness(crosscheck=lambda symbol: dec("100.5"))

    def tearDown(self):
        self.h.close()

    def _hold_with_spike(self):
        self.h.seed_position("NVDA", "1", "100", price="85")
        self.h.md.set_intraday("NVDA", near_intraday("100", self.h.now))
        self.h.quote("NVDA", 85)

    def test_mismatch_blocks_fake_stop_loss(self):
        self._hold_with_spike()

        report = self.h.cycle()

        self.assertEqual(report.sells, ())
        self.assertIn("NVDA:tick-guard-crosscheck-mismatch", report.blocked)
        self.assertIn("이상 틱 가드", self.h.alerts_text())
        self.assertIsNotNone(self.h.repo.position("NVDA"))

    def test_verified_spike_sells(self):
        self._hold_with_spike()
        self.h.engine.tick_crosscheck = lambda symbol: dec("85.1")

        report = self.h.cycle()

        self.assertEqual(len(report.sells), 1)
        record = [r for r in self.h.orders() if r.side == "SELL"][0]
        self.assertEqual(record.reason, "stop_loss")

    def test_guard_unit_cases(self):
        guard = TickGuard(self.h.guards.data)
        bars = near_intraday("100", self.h.now)
        ok = guard.check("NVDA", dec("99"), bars, self.h.now, None)
        self.assertTrue(ok.allowed)  # ±10% 미만은 통과
        self.assertFalse(ok.detail["spike"])
        blocked = guard.check("NVDA", dec("85"), bars, self.h.now, lambda s: dec("100.4"))
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, "tick-guard-crosscheck-mismatch")
        unavailable = guard.check("NVDA", dec("85"), bars, self.h.now, None)
        self.assertFalse(unavailable.allowed)
        self.assertEqual(unavailable.reason, "tick-guard-crosscheck-unavailable")
        confirmed = guard.check("NVDA", dec("85"), bars, self.h.now, lambda s: dec("84.9"))
        self.assertTrue(confirmed.allowed)
        self.assertTrue(confirmed.detail["verified"])


# ====================================================================== TC-811
class TC811PreemptiveLiquidation(unittest.TestCase):
    """14:52 ET 시점 물타기 포지션 수익률 -3.2% 도달 → 15:00 ET 소수점 마감 전 선제 청산."""

    def setUp(self):
        self.h = Harness(now=et(2026, 12, 1, 14, 52))

    def tearDown(self):
        self.h.close()

    def test_near_stop_position_liquidated_preemptively(self):
        self.h.seed_position("NVDA", "1", "100", price="96.8", add_count=1, entry_at=et(2026, 12, 1, 11, 50))
        self.h.md.set_intraday("NVDA", near_intraday("96.8", self.h.now))
        self.h.quote("NVDA", 96.8)  # -3.2% (물타기 손절 -3.5% 직전)

        report = self.h.cycle()

        self.assertEqual(len(report.sells), 1)
        record = [r for r in self.h.orders() if r.side == "SELL"][0]
        self.assertEqual(record.reason, "eod_preemptive_stop")
        self.assertIsNone(self.h.repo.position("NVDA"))

    def test_outside_window_keeps_position(self):
        self.h.clock.set(et(2026, 12, 1, 14, 40))
        self.h.seed_position("NVDA", "1", "100", price="96.8", add_count=1, entry_at=et(2026, 12, 1, 11, 50))
        self.h.md.set_intraday("NVDA", near_intraday("96.8", self.h.now))
        self.h.quote("NVDA", 96.8)

        report = self.h.cycle()

        self.assertEqual(report.sells, ())
        self.assertIsNotNone(self.h.repo.position("NVDA"))


# ====================================================================== TC-812
class TC812PremarketQueue(unittest.TestCase):
    """ALLOW_FRACTIONAL_PREMARKET_SELL=False 에서 +8.5% 갭상승 → 09:30~09:32 ET 예약 매도 큐 등록."""

    def setUp(self):
        self.h = Harness(cash="280", now=et(2026, 12, 1, 8, 0), premarket=False)

    def tearDown(self):
        self.h.close()

    def test_premarket_signal_is_queued_not_sent(self):
        self.h.seed_position("NVDA", "1", "100", price="108.5")
        self.h.quote("NVDA", 108.5)

        report = self.h.cycle()

        self.assertEqual(report.skipped, "before-open")
        self.assertEqual(report.sells, ())
        self.assertIn("premarket-queued:NVDA", report.notes)
        queue = self.h.repo.kv_get_json("premarket_sell_queue")
        self.assertEqual([item["symbol"] for item in queue], ["NVDA"])
        self.assertEqual(queue[0]["reason"], "take_profit")
        self.assertEqual(self.h.orders(), [])

    def test_queue_is_flushed_right_after_open(self):
        self.h.seed_position("NVDA", "1", "100", price="108.5")
        self.h.quote("NVDA", 108.5)
        self.h.cycle()

        self.h.clock.set(et(2026, 12, 1, 9, 31))
        self.h.quote("NVDA", 108.5)
        report = self.h.cycle()

        self.assertEqual(len(report.sells), 1)
        self.assertIsNone(self.h.repo.position("NVDA"))
        self.assertEqual(self.h.repo.kv_get_json("premarket_sell_queue"), [])

    def test_flag_true_sells_in_premarket(self):
        h = Harness(cash="280", now=et(2026, 12, 1, 8, 0), premarket=True)
        try:
            h.seed_position("NVDA", "1", "100", price="108.5")
            h.quote("NVDA", 108.5)
            report = h.cycle()
            self.assertEqual(len(report.sells), 1)
            self.assertIsNone(h.repo.position("NVDA"))
        finally:
            h.close()


# ====================================================================== TC-813
class TC813AccountKillSwitch(unittest.TestCase):
    """일손실 -4.1%, 누적 MDD -12.1%, API 3회 연속 오류 → 계좌 Kill Switch 발동 → 신규 주문 전면 정지."""

    def setUp(self):
        self.h = Harness(cash="300")

    def tearDown(self):
        self.h.close()

    def test_daily_loss_stops_every_order(self):
        # 현금 300 + NVDA 0.93 @100 → 현재가 90 → 평가액 83.7 → 총자산 383.7 (전일 기준 400 대비 -4.07%)
        self.h.seed_position("NVDA", "0.93", "100", price="90")
        self.h.quote("NVDA", 90)
        self.h.repo.ensure_day_state(self.h.now.date(), dec("400"), self.h.now)

        report = self.h.cycle()

        self.assertEqual(report.sells, ())
        self.assertEqual(report.buys, ())
        self.assertIn("kill-switch:daily-loss", report.blocked)
        self.assertTrue(self.h.engine.kill_switch.active)
        self.assertIn("Kill Switch", self.h.alerts_text())
        # 발동 뒤에는 다음 사이클도 계속 정지
        report = self.h.cycle()
        self.assertEqual(report.orders, ())

    def test_mdd_stops_every_order(self):
        self.h.repo.kv_set("equity_peak", "500")
        self.h.halt_file  # noqa: B018 - 파일은 만들지 않는다
        self.h.broker.cash = dec("439")

        report = self.h.cycle()

        self.assertEqual(report.buys, ())
        self.assertIn("kill-switch:max-drawdown", report.blocked)
        self.assertTrue(self.h.engine.kill_switch.active)

    def test_three_api_errors_trip(self):
        for _ in range(3):
            self.h.engine._note_api_failure(self.h.now, "test", RuntimeError("api down"))

        self.assertTrue(self.h.engine.kill_switch.active)
        report = self.h.cycle()
        self.assertEqual(report.buys, ())
        self.assertIn("kill-switch:api-errors", report.blocked)
        self.assertIn("Kill Switch", self.h.alerts_text())

    def test_duplicate_order_attempt_trips(self):
        self.h.cycle()  # 10:30 NVDA 매수
        # 5분 안에 같은 종목·방향 주문 시도 → Safe Halt
        self.h.clock.advance(120)
        decision = self.h.engine.kill_switch.note_order_attempt("NVDA", "BUY", self.h.clock.now())
        self.assertFalse(decision.allowed)
        self.assertTrue(self.h.engine.kill_switch.active)
        self.assertEqual(self.h.engine.kill_switch.reason(), "duplicate-order")

    def test_manual_reset(self):
        self.h.broker.cash = dec("439")
        self.h.repo.kv_set("equity_peak", "500")
        self.h.cycle()
        self.assertTrue(self.h.engine.kill_switch.active)
        self.h.engine.kill_switch.reset(self.h.clock.now())
        self.assertFalse(self.h.engine.kill_switch.active)


# ====================================================================== 기타 v3.3 규칙
class OvernightGapStopTests(unittest.TestCase):
    """오버나잇 갭하락 손절은 09:32 ET 이후에 집행한다."""

    def setUp(self):
        self.h = Harness(cash="280", now=et(2026, 12, 1, 9, 31))

    def tearDown(self):
        self.h.close()

    def test_stop_deferred_before_0932_and_run_after(self):
        self.h.seed_position(
            "NVDA", "1", "100", price="94",
            entry_at=et(2026, 11, 30, 10), first_entry_date=date(2026, 11, 30),
        )
        self.h.quote("NVDA", 94)  # -6.0% 갭하락

        report = self.h.cycle()
        self.assertEqual(report.sells, ())
        self.assertIn("NVDA:overnight-stop-deferred", report.blocked)
        self.assertIsNotNone(self.h.repo.position("NVDA"))

        self.h.clock.set(et(2026, 12, 1, 9, 33))
        self.h.quote("NVDA", 94)
        report = self.h.cycle()
        self.assertEqual(len(report.sells), 1)
        record = [r for r in self.h.orders() if r.side == "SELL"][0]
        self.assertEqual(record.reason, "stop_loss")


class EarlyClosePreemptiveTests(unittest.TestCase):
    """조기폐장일(13:00 ET) 선제 청산 창은 12:50~12:58 ET."""

    def setUp(self):
        self.h = Harness(now=et(2026, 12, 24, 12, 52))

    def tearDown(self):
        self.h.close()

    def test_early_close_preemptive_window(self):
        self.h.seed_position("NVDA", "1", "100", price="96.8", add_count=1, entry_at=et(2026, 12, 24, 11, 50))
        self.h.md.set_intraday("NVDA", near_intraday("96.8", self.h.now))
        self.h.quote("NVDA", 96.8)

        report = self.h.cycle()

        self.assertEqual(len(report.sells), 1)
        record = [r for r in self.h.orders() if r.side == "SELL"][0]
        self.assertEqual(record.reason, "eod_preemptive_stop")


class GfvReentryTests(unittest.TestCase):
    """현금 계좌: 매도대금 미결제(T+1) 중 같은 종목 재매수는 GFV 방지로 차단."""

    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_same_day_rebuy_blocked_after_sell(self):
        self.h.cycle()  # 10:30 진입
        self.h.clock.advance(300)  # 10:35
        self.h.quote("NVDA", 103.68)  # +8% 익절
        self.h.cycle()

        self.h.clock.advance(16 * 60)  # 15분 쿨다운 경과
        self.h.quote("NVDA", 96)
        report = self.h.cycle()

        self.assertEqual(report.buys, ())
        self.assertIn("NVDA:gfv-unsettled-funds", report.blocked)
        self.assertEqual(self.h.repo.kv_get_json("unsettled_proceeds")[0]["symbol"], "NVDA")


class SizingTierTests(unittest.TestCase):
    def test_three_tiers(self):
        capital = load_strategy(ROOT / "config" / "strategy.yaml").capital
        self.assertEqual(capital.tier(dec("399.99")), (dec("75.00"), dec("150.00")))
        self.assertEqual(capital.tier(dec("400")), (dec("85.00"), dec("170.00")))
        self.assertEqual(capital.tier(dec("499.99")), (dec("85.00"), dec("170.00")))
        self.assertEqual(capital.tier(dec("500")), (dec("120.00"), dec("240.00")))

    def test_plan_b_keeps_single_under_500_tier(self):
        capital = replace(load_strategy(ROOT / "config" / "strategy.yaml").capital, plan="B")
        self.assertEqual(capital.tier(dec("380")), (dec("85.00"), dec("170.00")))
        self.assertEqual(capital.tier(dec("450")), (dec("85.00"), dec("170.00")))
        self.assertEqual(capital.tier(dec("500")), (dec("120.00"), dec("240.00")))


class SignalFailClosedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(Database(Path(self.tmp.name) / "state.db"))
        self.book = SignalBook(self.repo, dec("25"))
        self.now = et(2026, 12, 1, 10, 30)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_unknown_rsi_discards_signal(self):
        self.book.publish("NVDA", self.now, 900, dec("3"), dec("1"))
        changes = self.book.housekeeping(self.now + timedelta(seconds=60), {"NVDA": None})
        self.assertEqual(changes, [("NVDA", STATUS_DISCARDED)])
        self.assertEqual(len(self.repo.signals("PENDING")), 0)

    def test_recovered_rsi_discards_signal(self):
        self.book.publish("NVDA", self.now, 900, dec("3"), dec("1"))
        changes = self.book.housekeeping(self.now + timedelta(seconds=60), {"NVDA": dec("30")})
        self.assertEqual(changes, [("NVDA", STATUS_DISCARDED)])

    def test_expired_signal(self):
        self.book.publish("NVDA", self.now, 900, dec("3"), dec("1"))
        changes = self.book.housekeeping(self.now + timedelta(seconds=901), {"NVDA": dec("10")})
        self.assertEqual(changes, [("NVDA", "EXPIRED")])


class AccountRegulationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(Database(Path(self.tmp.name) / "state.db"))
        self.guards = load_guards(ROOT / "config" / "guards.yaml")
        self.calendar = StaticUsCalendar(ROOT / "config" / "market_calendar_2026.json")
        self.today = date(2026, 12, 1)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_unsettled_proceeds_reduce_settled_cash(self):
        guard = AccountRegulationGuard(self.repo, self.guards.account, self.calendar)
        guard.record_sell("NVDA", dec("100"), self.today)
        self.assertEqual(guard.unsettled_total(self.today), dec("100"))
        self.assertEqual(guard.settled_cash(dec("380"), self.today), dec("280"))
        self.assertTrue(guard.rebuy_blocked("NVDA", self.today))
        self.assertFalse(guard.rebuy_blocked("AAPL", self.today))
        # T+1(다음 거래일) 결제 후 해제
        self.assertEqual(guard.unsettled_total(date(2026, 12, 2)), ZERO)
        self.assertFalse(guard.rebuy_blocked("NVDA", date(2026, 12, 2)))

    def test_margin_account_ignores_settlement(self):
        config = replace(self.guards.account, settlement="margin")
        guard = AccountRegulationGuard(self.repo, config, self.calendar)
        guard.record_sell("NVDA", dec("100"), self.today)
        self.assertEqual(guard.unsettled_total(self.today), ZERO)

    def test_pdt_blocks_after_three_day_trades(self):
        config = replace(self.guards.account, settlement="margin")
        guard = AccountRegulationGuard(self.repo, config, self.calendar)
        self.assertTrue(guard.pdt_status(self.today, dec("1000")).allowed)
        for _ in range(3):
            guard.record_day_trade("NVDA", self.today, et(2026, 12, 1, 10))
        decision = guard.pdt_status(self.today, dec("1000"))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "pdt-limit")
        # 자산 $25,000 이상이면 PDT 미적용
        self.assertTrue(guard.pdt_status(self.today, dec("30000")).allowed)


class TrailingExitTests(unittest.TestCase):
    def test_time_exit_switches_to_trailing(self):
        cfg = load_strategy(ROOT / "config" / "strategy.yaml").exit
        position = Position("NVDA", dec("1"), dec("100"), et(2026, 11, 16, 10), date(2026, 11, 16), dec("8"))
        armed = evaluate_exit(position, dec("103.5"), cfg, trading_days_held=10, calendar_days_held=13)
        self.assertFalse(armed.should_exit)
        self.assertTrue(armed.arm_trailing)
        self.assertEqual(armed.reason, "trailing_armed")

        trail = evaluate_exit(
            position, dec("101.4"), cfg, trading_days_held=10, calendar_days_held=13, trail_high=dec("103.5")
        )
        self.assertTrue(trail.should_exit)
        self.assertEqual(trail.reason, "trailing_stop")
        self.assertEqual(trail.trigger_price, dec("101.4300"))

        still = evaluate_exit(
            position, dec("102.0"), cfg, trading_days_held=10, calendar_days_held=13, trail_high=dec("103.5")
        )
        self.assertFalse(still.should_exit)


if __name__ == "__main__":
    unittest.main()
