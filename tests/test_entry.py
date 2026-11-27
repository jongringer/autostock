import unittest
from datetime import date
from decimal import Decimal

from helpers import ROOT, d, et

from alpha_shield.config import load_strategy
from alpha_shield.models import Position
from alpha_shield.strategy.entry import (
    SymbolInputs,
    drawdown_from_high,
    entry_threshold_pct,
    evaluate_add,
    evaluate_entry,
)


class EntryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        strategy = load_strategy(ROOT / "config" / "strategy.yaml")
        cls.entry = strategy.entry
        cls.add = strategy.add
        cls.universe = strategy.universe

    def inputs(self, **overrides):
        base = dict(
            symbol="NVDA",
            last_price=d("96.00"),
            hod=d("100.50"),
            rsi2=d("8.00"),
            atr_pct=d("2.00"),
            sma200=d("90.00"),
        )
        base.update(overrides)
        return SymbolInputs(**base)

    # ------------------------------------------------------------------ 진입
    def test_entry_passes_all_conditions(self):
        evaluation = evaluate_entry(self.inputs(), self.entry, self.universe)
        self.assertTrue(evaluation.allowed, evaluation.reasons)
        self.assertEqual(evaluation.threshold_pct, d("1.04"))
        self.assertEqual(evaluation.drawdown_pct, d("-4.4776"))
        # score = 1.5 x min(4.4776 / 1.04, 2.5) — v3.3 낙폭 비율 2.5배 상한 캡
        self.assertEqual(evaluation.score, d("3.75"))

    def test_entry_blocked_without_enough_dip(self):
        evaluation = evaluate_entry(self.inputs(last_price=d("100.00"), hod=d("100.50")), self.entry, self.universe)
        self.assertFalse(evaluation.allowed)
        self.assertEqual(evaluation.reason, "no-dip")

    def test_rsi_too_high_blocks(self):
        evaluation = evaluate_entry(self.inputs(rsi2=d("20.00")), self.entry, self.universe)
        self.assertFalse(evaluation.allowed)
        self.assertIn("rsi-too-high", evaluation.reasons)

    def test_chase_block_at_70(self):
        evaluation = evaluate_entry(self.inputs(rsi2=d("70.00"), last_price=d("96")), self.entry, self.universe)
        self.assertFalse(evaluation.allowed)
        self.assertIn("chase-block", evaluation.reasons)

    def test_sma_floor_blocks(self):
        evaluation = evaluate_entry(self.inputs(sma200=d("130.00")), self.entry, self.universe)
        self.assertFalse(evaluation.allowed)
        self.assertIn("below-sma-floor", evaluation.reasons)

    def test_missing_rsi_fails_closed(self):
        evaluation = evaluate_entry(self.inputs(rsi2=None), self.entry, self.universe)
        self.assertFalse(evaluation.allowed)
        self.assertEqual(evaluation.reason, "rsi-unavailable")

    def test_missing_sma_fails_closed(self):
        evaluation = evaluate_entry(self.inputs(sma200=None), self.entry, self.universe)
        self.assertFalse(evaluation.allowed)
        self.assertEqual(evaluation.reason, "sma200-unavailable")

    # ------------------------------------------------------------------ ATR 폴백
    def test_atr_fallback_qqq(self):
        evaluation = evaluate_entry(
            self.inputs(symbol="QQQ", last_price=d("99.25"), hod=d("100"), atr_pct=None),
            self.entry,
            self.universe,
        )
        self.assertTrue(evaluation.used_fallback)
        self.assertEqual(evaluation.threshold_pct, d("0.7"))
        self.assertTrue(evaluation.allowed)

    def test_atr_fallback_aapl_needs_one_percent(self):
        evaluation = evaluate_entry(
            self.inputs(symbol="AAPL", last_price=d("99.35"), hod=d("100"), atr_pct=None),
            self.entry,
            self.universe,
        )
        self.assertEqual(evaluation.threshold_pct, d("1.0"))
        self.assertFalse(evaluation.allowed)

    def test_atr_fallback_amzn_and_default(self):
        amzn = evaluate_entry(
            self.inputs(symbol="AMZN", last_price=d("98.6"), hod=d("100"), atr_pct=None),
            self.entry,
            self.universe,
        )
        self.assertEqual(amzn.threshold_pct, d("1.3"))
        self.assertTrue(amzn.allowed)
        nvda = evaluate_entry(
            self.inputs(symbol="NVDA", last_price=d("98.6"), hod=d("100"), atr_pct=None),
            self.entry,
            self.universe,
        )
        self.assertEqual(nvda.threshold_pct, d("1.5"))
        self.assertFalse(nvda.allowed)

    # ------------------------------------------------------------------ 거시 강화
    def test_macro_boost_adds_half_point(self):
        inputs = self.inputs(last_price=d("98.7"), hod=d("100"))  # -1.3%
        normal = evaluate_entry(inputs, self.entry, self.universe)
        boosted = evaluate_entry(inputs, self.entry, self.universe, extra_drawdown_pct=d("0.5"))
        self.assertTrue(normal.allowed)
        self.assertFalse(boosted.allowed)
        self.assertEqual(boosted.threshold_pct, d("1.54"))

    # ------------------------------------------------------------------ 물타기
    def position(self, add_count=0, avg="100"):
        return Position("NVDA", d("0.85"), d(avg), et(2026, 11, 30, 10), date(2026, 11, 30), d("8"), add_count=add_count)

    def test_add_allowed_on_dip(self):
        evaluation = evaluate_add(
            self.position(), self.add, last_price=d("98.00"), rsi2=d("30"), atr_pct=d("1.0"),
            now=et(2026, 11, 30, 12, 30),
        )
        self.assertTrue(evaluation.allowed)
        self.assertEqual(evaluation.gap_pct, d("-2.0000"))

    def test_add_requires_minus_1_5_percent(self):
        evaluation = evaluate_add(
            self.position(), self.add, last_price=d("99.00"), rsi2=d("30"), atr_pct=d("1.0"),
            now=et(2026, 11, 30, 12, 30),
        )
        self.assertFalse(evaluation.allowed)
        self.assertEqual(evaluation.reason, "no-add-dip")

    def test_add_only_once(self):
        evaluation = evaluate_add(
            self.position(add_count=1), self.add, last_price=d("98.00"), rsi2=d("30"), atr_pct=d("1.0"),
            now=et(2026, 11, 30, 12, 30),
        )
        self.assertFalse(evaluation.allowed)
        self.assertEqual(evaluation.reason, "max-adds")

    def test_add_rsi_limit(self):
        evaluation = evaluate_add(
            self.position(), self.add, last_price=d("98.00"), rsi2=d("36"), atr_pct=d("1.0"),
            now=et(2026, 11, 30, 12, 30),
        )
        self.assertFalse(evaluation.allowed)
        self.assertEqual(evaluation.reason, "rsi-too-high")

    def test_add_blocked_before_two_hours(self):
        evaluation = evaluate_add(
            self.position(), self.add, last_price=d("98.00"), rsi2=d("30"), atr_pct=d("1.0"),
            now=et(2026, 11, 30, 10, 30),
        )
        self.assertFalse(evaluation.allowed)
        self.assertEqual(evaluation.reason, "add-interval")

    def test_add_interval_cleared_next_trading_day(self):
        evaluation = evaluate_add(
            self.position(), self.add, last_price=d("98.00"), rsi2=d("30"), atr_pct=d("1.0"),
            now=et(2026, 12, 1, 10, 0),
        )
        self.assertTrue(evaluation.allowed, evaluation.reason)

    def test_add_trigger_uses_atr_when_larger(self):
        # ATR 3% -> 요구 하락 2.4%. -2.0% 면 부족하다.
        evaluation = evaluate_add(
            self.position(), self.add, last_price=d("98.00"), rsi2=d("30"), atr_pct=d("3.0"),
            now=et(2026, 11, 30, 12, 30),
        )
        self.assertFalse(evaluation.allowed)
        self.assertEqual(evaluation.reason, "no-add-dip")
        self.assertEqual(evaluation.required_pct, d("2.4"))

    def test_add_without_clock_fails_closed(self):
        evaluation = evaluate_add(self.position(), self.add, last_price=d("98.00"), rsi2=d("30"))
        self.assertFalse(evaluation.allowed)
        self.assertEqual(evaluation.reason, "add-interval-unconfirmed")

    def test_drawdown_helper(self):
        self.assertEqual(drawdown_from_high(d("95"), d("100")), d("-5.0000"))


if __name__ == "__main__":
    unittest.main()
