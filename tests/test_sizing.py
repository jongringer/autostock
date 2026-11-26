import unittest
from datetime import date
from decimal import Decimal

from helpers import ROOT, d, et

from alpha_shield.config import load_strategy
from alpha_shield.models import Position
from alpha_shield.strategy.sizing import (
    Budget,
    buy_quantity_for_amount,
    cash_buffer,
    plan_add,
    plan_new_entry,
    spendable_cash,
    tier_for,
)


class SizingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = load_strategy(ROOT / "config" / "strategy.yaml").capital

    def test_tier_boundary(self):
        self.assertEqual(tier_for(d("499.99"), self.cfg).slot_amount, d("85.00"))
        self.assertEqual(tier_for(d("499.99"), self.cfg).symbol_cap, d("170.00"))
        self.assertEqual(tier_for(d("500.00"), self.cfg).slot_amount, d("120.00"))
        self.assertEqual(tier_for(d("500.01"), self.cfg).symbol_cap, d("240.00"))

    def test_cash_buffer_floor_and_ratio(self):
        self.assertEqual(cash_buffer(d("300"), self.cfg), d("55.00"))
        self.assertEqual(cash_buffer(d("500"), self.cfg), d("75"))
        self.assertEqual(cash_buffer(d("366.66"), self.cfg), d("55"))
        self.assertGreater(cash_buffer(d("366.67"), self.cfg), d("55"))

    def test_spendable_cash_never_negative(self):
        self.assertEqual(spendable_cash(d("380"), d("40"), self.cfg), Decimal("0"))
        self.assertEqual(spendable_cash(d("380"), d("150"), self.cfg), d("93"))

    def test_plan_new_entry_uses_slot_amount(self):
        budget = Budget(d("100"))
        self.assertEqual(plan_new_entry(d("380"), d("380"), self.cfg, budget), d("85.00"))
        self.assertEqual(budget.remaining, d("15.00"))

    def test_plan_new_entry_respects_buffer(self):
        # 현금 120 - 버퍼 max(380x15%=57, 55)=57 -> 63 (최소주문 10 이상)
        budget = Budget(d("500"))
        self.assertEqual(plan_new_entry(d("380"), d("120"), self.cfg, budget), d("63.00"))

    def test_plan_new_entry_below_min_order(self):
        budget = Budget(d("500"))
        self.assertIsNone(plan_new_entry(d("380"), d("60"), self.cfg, budget))  # 5 < 10

    def test_plan_new_entry_budget_limited(self):
        budget = Budget(d("12.34"))
        self.assertEqual(plan_new_entry(d("380"), d("380"), self.cfg, budget), d("12.34"))
        self.assertEqual(budget.remaining, Decimal("0"))

    def test_plan_add_capped_by_symbol_cap(self):
        position = Position("NVDA", d("1"), d("100"), et(2026, 11, 30, 10), date(2026, 11, 30), d("8"))
        budget = Budget(d("500"))
        # 노출 1 x 100 = 100, 상한 170 -> 70
        self.assertEqual(plan_add(position, d("100"), d("380"), d("380"), self.cfg, budget), d("70.00"))

    def test_plan_add_no_room(self):
        position = Position("NVDA", d("2"), d("85"), et(2026, 11, 30, 10), date(2026, 11, 30), d("8"))
        budget = Budget(d("500"))
        self.assertIsNone(plan_add(position, d("100"), d("380"), d("380"), self.cfg, budget))

    def test_buy_quantity_rounds_down_six(self):
        self.assertEqual(buy_quantity_for_amount(d("85"), d("3")), Decimal("28.333333"))


if __name__ == "__main__":
    unittest.main()
