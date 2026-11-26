import unittest
from datetime import date, timedelta
from decimal import Decimal

from helpers import (
    BlockingFilings,
    EmptyNews,
    FixedEarnings,
    FixedNews,
    MutableMarketData,
    ROOT,
    d,
    et,
    make_daily,
)

from alpha_shield.calendar import SessionGuard, StaticUsCalendar
from alpha_shield.config import load_guards
from alpha_shield.guards.data import DataGuard
from alpha_shield.guards.events import EarningsGuard
from alpha_shield.guards.filings import FilingsGuard
from alpha_shield.guards.macro import MacroGuard
from alpha_shield.guards.market import MarketGuard
from alpha_shield.guards.news import NewsGuard
from alpha_shield.marketdata.macro import MacroCalendar
from alpha_shield.models import Quote


class MarketGuardTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_guards(ROOT / "config" / "guards.yaml").market
        self.md = MutableMarketData()

    def test_allows_normal_market(self):
        self.assertTrue(MarketGuard(self.md, self.cfg).check().allowed)

    def test_blocks_nq_crash(self):
        self.md.set_futures(d("-1.5"))
        decision = MarketGuard(self.md, self.cfg).check()
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "nq-crash")

    def test_blocks_vix_panic(self):
        self.md.set_vix(d("30"))
        decision = MarketGuard(self.md, self.cfg).check()
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "vix-panic")

    def test_missing_data_fails_closed(self):
        self.md.set_vix(None)
        decision = MarketGuard(self.md, self.cfg).check()
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "market-data-unavailable")

    def test_missing_data_can_be_tolerated(self):
        from dataclasses import replace

        cfg = replace(self.cfg, fail_closed=False)
        self.md.set_futures(None)
        self.assertTrue(MarketGuard(self.md, cfg).check().allowed)


class SessionGuardTests(unittest.TestCase):
    def setUp(self):
        self.calendar = StaticUsCalendar(ROOT / "config" / "market_calendar_2026.json")
        self.guard = SessionGuard(self.calendar, load_guards(ROOT / "config" / "guards.yaml"))

    def state(self, moment):
        return self.guard.evaluate(moment)

    def test_before_open(self):
        self.assertEqual(self.state(et(2026, 12, 1, 9, 29)).reason, "before-open")

    def test_opening_grace(self):
        state = self.state(et(2026, 12, 1, 9, 40))
        self.assertTrue(state.is_open)
        self.assertFalse(state.entry_allowed)
        self.assertEqual(state.reason, "opening-grace")
        # 개장 직후에도 청산(매도)은 가능해야 한다
        self.assertTrue(state.fractional_sell_allowed)

    def test_entry_starts_at_0945(self):
        state = self.state(et(2026, 12, 1, 9, 45))
        self.assertTrue(state.entry_allowed)
        self.assertTrue(state.fractional_sell_allowed)

    def test_closing_grace_starts_at_1445(self):
        state = self.state(et(2026, 12, 1, 14, 45))
        self.assertFalse(state.entry_allowed)
        self.assertEqual(state.reason, "closing-grace")
        self.assertTrue(state.fractional_sell_allowed)

    def test_fractional_sell_closes_at_1500(self):
        state = self.state(et(2026, 12, 1, 15, 0))
        self.assertFalse(state.fractional_sell_allowed)
        self.assertTrue(state.is_open)

    def test_after_close(self):
        self.assertEqual(self.state(et(2026, 12, 1, 16, 0)).reason, "after-close")

    def test_early_close_day(self):
        # 2026-11-27 (추수감사절 다음날) 은 13:00 ET 마감
        state = self.state(et(2026, 11, 27, 12, 46))
        self.assertEqual(state.reason, "closing-grace")
        self.assertFalse(state.fractional_sell_allowed)
        self.assertFalse(self.state(et(2026, 11, 27, 12, 20)).entry_allowed)

    def test_holiday(self):
        self.assertFalse(self.state(et(2026, 11, 26, 11, 0)).is_open)

    def test_window_times(self):
        window = self.calendar.session_on(date(2026, 12, 1))
        self.assertEqual(window.regular_start.hour, 9)
        self.assertEqual(window.regular_end.hour, 16)


class DataGuardTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_guards(ROOT / "config" / "guards.yaml").data
        self.guard = DataGuard(self.cfg)
        self.now = et(2026, 12, 1, 10, 30)
        self.bars = make_daily([100] * 210, last_day=date(2026, 11, 30))

    def test_ok(self):
        quote = Quote("NVDA", d("100"), self.now - timedelta(seconds=30))
        self.assertTrue(self.guard.check("NVDA", quote, self.bars, self.now).allowed)

    def test_stale_quote_blocks(self):
        quote = Quote("NVDA", d("100"), self.now - timedelta(seconds=901))
        decision = self.guard.check("NVDA", quote, self.bars, self.now)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "quote-stale")

    def test_quote_without_timestamp_blocks(self):
        decision = self.guard.check("NVDA", Quote("NVDA", d("100"), None), self.bars, self.now)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "quote-timestamp-missing")

    def test_missing_quote_blocks(self):
        self.assertEqual(self.guard.check("NVDA", None, self.bars, self.now).reason, "quote-missing")

    def test_not_enough_bars_for_sma200(self):
        quote = Quote("NVDA", d("100"), self.now - timedelta(seconds=30))
        decision = self.guard.check("NVDA", quote, self.bars[:199], self.now)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "sma200-unavailable")

    def test_stale_daily_bars_block(self):
        quote = Quote("NVDA", d("100"), self.now - timedelta(seconds=30))
        old = make_daily([100] * 210, last_day=date(2026, 11, 20))
        decision = self.guard.check("NVDA", quote, old, self.now)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "daily-bars-stale")


class EarningsGuardTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_guards(ROOT / "config" / "guards.yaml").events
        self.calendar = StaticUsCalendar(ROOT / "config" / "market_calendar_2026.json")

    def test_earnings_today_blocks(self):
        today = date(2026, 12, 1)
        guard = EarningsGuard(FixedEarnings({today: {"NVDA"}}), self.cfg, self.calendar)
        decisions = guard.decisions(["NVDA", "AAPL"], today)
        self.assertFalse(decisions["NVDA"].allowed)
        self.assertEqual(decisions["NVDA"].reason, "earnings-today")
        self.assertTrue(decisions["AAPL"].allowed)

    def test_earnings_tomorrow_blocks(self):
        today = date(2026, 12, 1)
        guard = EarningsGuard(FixedEarnings({date(2026, 12, 2): {"TSLA"}}), self.cfg, self.calendar)
        self.assertEqual(guard.decisions(["TSLA"], today)["TSLA"].reason, "earnings-tomorrow")

    def test_friday_checks_monday(self):
        today = date(2026, 12, 4)  # 금요일
        guard = EarningsGuard(FixedEarnings({date(2026, 12, 7): {"NVDA"}}), self.cfg, self.calendar)
        self.assertEqual(guard.decisions(["NVDA"], today)["NVDA"].reason, "earnings-tomorrow")

    def test_unavailable_fails_closed(self):
        guard = EarningsGuard(FixedEarnings(fail=True), self.cfg, self.calendar)
        decisions = guard.decisions(["NVDA", "AAPL"], date(2026, 12, 1))
        self.assertFalse(decisions["NVDA"].allowed)
        self.assertEqual(decisions["NVDA"].reason, "earnings-calendar-unavailable")


class FilingsGuardTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_guards(ROOT / "config" / "guards.yaml").filings

    def test_blocks_adverse_8k(self):
        guard = FilingsGuard(BlockingFilings(blocked=True), self.cfg)
        decision = guard.decisions(["NVDA"], date(2026, 12, 1))["NVDA"]
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "sec-8k-adverse")

    def test_unavailable_blocks_when_configured(self):
        guard = FilingsGuard(BlockingFilings(checked=False), self.cfg)
        decision = guard.decisions(["NVDA"], date(2026, 12, 1))["NVDA"]
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "sec-unavailable")

    def test_symbol_without_cik_is_skipped(self):
        guard = FilingsGuard(BlockingFilings(blocked=True), self.cfg)
        decision = guard.decisions(["QQQ"], date(2026, 12, 1))["QQQ"]
        self.assertTrue(decision.allowed)


class NewsGuardTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_guards(ROOT / "config" / "guards.yaml").news

    def test_blocks_on_keyword(self):
        guard = NewsGuard(FixedNews(["NVDA files for bankruptcy protection"]), self.cfg)
        decision = guard.decisions(["NVDA"])["NVDA"]
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "news-block")

    def test_unavailable_fails_closed(self):
        guard = NewsGuard(FixedNews(fail=True), self.cfg)
        self.assertEqual(guard.decisions(["NVDA"])["NVDA"].reason, "news-unavailable")

    def test_clean_headlines_pass(self):
        guard = NewsGuard(EmptyNews(), self.cfg)
        self.assertTrue(guard.decisions(["NVDA"])["NVDA"].allowed)

    def test_disabled(self):
        from dataclasses import replace

        cfg = replace(self.cfg, enabled=False)
        guard = NewsGuard(FixedNews(fail=True), cfg)
        self.assertTrue(guard.decisions(["NVDA"])["NVDA"].allowed)


class MacroGuardTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_guards(ROOT / "config" / "guards.yaml").macro
        self.calendar = MacroCalendar(ROOT / "config" / "macro_calendar_2026.json", d("0.5"))

    def test_boost_on_fomc_day(self):
        guard = MacroGuard(self.calendar, self.cfg)
        self.assertEqual(guard.drawdown_boost_pct(date(2026, 12, 9)), d("0.5"))
        self.assertEqual(guard.drawdown_boost_pct(date(2026, 12, 11)), d("0"))

    def test_events_on(self):
        guard = MacroGuard(self.calendar, self.cfg)
        self.assertIn("FOMC", guard.events_on(date(2026, 12, 9)))

    def test_missing_calendar_fails_closed(self):
        guard = MacroGuard(None, self.cfg)
        self.assertFalse(guard.decision().allowed)
        self.assertEqual(guard.decision().reason, "macro-calendar-unavailable")


if __name__ == "__main__":
    unittest.main()
