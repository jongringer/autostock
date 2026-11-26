import unittest
from datetime import date
from decimal import Decimal

from helpers import ROOT, d, et

from alpha_shield.config import load_strategy
from alpha_shield.models import Position
from alpha_shield.strategy.exit import evaluate_exit, stop_loss_pct, vix_take_profit_pct


class ExitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = load_strategy(ROOT / "config" / "strategy.yaml").exit

    def position(self, **overrides):
        base = dict(
            symbol="NVDA",
            qty=d("0.8"),
            avg_price=d("100"),
            entry_at=et(2026, 12, 1, 10),
            first_entry_date=date(2026, 12, 1),
            tp_target_pct=d("8"),
            entry_vix=d("15"),
            add_count=0,
        )
        base.update(overrides)
        return Position(**base)

    def test_stop_loss_single_position(self):
        evaluation = evaluate_exit(self.position(), d("95"), self.cfg, trading_days_held=1, calendar_days_held=1)
        self.assertTrue(evaluation.should_exit)
        self.assertEqual(evaluation.reason, "stop_loss")

    def test_stop_loss_added_position_tighter(self):
        self.assertEqual(stop_loss_pct(self.position(), self.cfg), d("5.0"))
        self.assertEqual(stop_loss_pct(self.position(add_count=1), self.cfg), d("3.5"))
        evaluation = evaluate_exit(
            self.position(add_count=1), d("96.4"), self.cfg, trading_days_held=1, calendar_days_held=1
        )
        self.assertTrue(evaluation.should_exit)
        self.assertEqual(evaluation.reason, "stop_loss")

    def test_take_profit_low_vix_is_eight_percent(self):
        self.assertEqual(vix_take_profit_pct(d("15"), self.cfg), d("8.0"))
        evaluation = evaluate_exit(
            self.position(tp_target_pct=d("8")), d("108"), self.cfg, trading_days_held=1, calendar_days_held=1
        )
        self.assertTrue(evaluation.should_exit)
        self.assertEqual(evaluation.reason, "take_profit")
        self.assertFalse(
            evaluate_exit(self.position(), d("107.9"), self.cfg, trading_days_held=1, calendar_days_held=1).should_exit
        )

    def test_take_profit_high_or_unknown_vix_is_five(self):
        self.assertEqual(vix_take_profit_pct(d("18.0"), self.cfg), d("5.0"))
        self.assertEqual(vix_take_profit_pct(d("31"), self.cfg), d("5.0"))
        self.assertEqual(vix_take_profit_pct(None, self.cfg), d("5.0"))
        evaluation = evaluate_exit(
            self.position(tp_target_pct=d("5")), d("105"), self.cfg, trading_days_held=1, calendar_days_held=1
        )
        self.assertEqual(evaluation.reason, "take_profit")

    def test_time_exit_trading_days(self):
        evaluation = evaluate_exit(
            self.position(), d("100"), self.cfg, trading_days_held=10, calendar_days_held=13
        )
        self.assertTrue(evaluation.should_exit)
        self.assertEqual(evaluation.reason, "time_exit")

    def test_time_exit_calendar_days(self):
        evaluation = evaluate_exit(
            self.position(), d("100"), self.cfg, trading_days_held=9, calendar_days_held=14
        )
        self.assertEqual(evaluation.reason, "time_exit")

    def test_hold_within_limits(self):
        evaluation = evaluate_exit(
            self.position(), d("101"), self.cfg, trading_days_held=3, calendar_days_held=5
        )
        self.assertFalse(evaluation.should_exit)
        self.assertEqual(evaluation.reason, "hold")

    def test_stop_takes_priority_over_take_profit(self):
        # 두 조건이 동시에 성립할 수는 없지만, 손절 판정이 먼저라는 계약을 고정한다
        evaluation = evaluate_exit(self.position(), d("90"), self.cfg, trading_days_held=20, calendar_days_held=20)
        self.assertEqual(evaluation.reason, "stop_loss")


if __name__ == "__main__":
    unittest.main()
