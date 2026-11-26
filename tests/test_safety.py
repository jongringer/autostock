import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from helpers import ROOT, d, et

from alpha_shield.config import load_guards
from alpha_shield.engine.reconcile import reconcile
from alpha_shield.models import OrderRequest, OrderResult, Position, PositionView
from alpha_shield.safety import (
    DailyRiskGuard,
    HaltSwitch,
    LiveOrdersDisabled,
    LiveOrdersEnabledButNotArmed,
    OrderRateGuard,
    SafetyLock,
)
from alpha_shield.state.db import Database
from alpha_shield.state.repo import Repo


class SafetyLockTests(unittest.TestCase):
    def test_requires_env_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            armed = Path(tmp) / "ARMED"
            armed.touch()
            lock = SafetyLock(False, armed)
            self.assertFalse(lock.live_allowed)
            with self.assertRaises(LiveOrdersDisabled):
                lock.assert_live()

    def test_requires_armed_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = SafetyLock(True, Path(tmp) / "ARMED")
            self.assertFalse(lock.live_allowed)
            with self.assertRaises(LiveOrdersEnabledButNotArmed):
                lock.assert_live()

    def test_both_conditions(self):
        with tempfile.TemporaryDirectory() as tmp:
            armed = Path(tmp) / "ARMED"
            armed.touch()
            lock = SafetyLock(True, armed)
            self.assertTrue(lock.live_allowed)
            lock.assert_live()
            armed.unlink()
            self.assertFalse(lock.live_allowed)

    def test_halt_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            flag = Path(tmp) / "halt.flag"
            switch = HaltSwitch(flag)
            self.assertFalse(switch.halted)
            flag.write_text("수동 정지", encoding="utf-8")
            self.assertTrue(switch.halted)
            self.assertEqual(switch.reason(), "수동 정지")


class OrderRateGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(Database(Path(self.tmp.name) / "state.db"))
        self.config = load_guards(ROOT / "config" / "guards.yaml").risk
        self.guard = OrderRateGuard(self.repo, self.config)
        self.now = et(2026, 12, 1, 10, 30)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def record(self, symbol, side, at, status="FILLED"):
        cid = f"{symbol}-{side}-{at.timestamp()}"
        request = OrderRequest(symbol, side, cid, amount_usd=d("20") if side == "BUY" else None, qty=None if side == "BUY" else d("0.1"))
        self.repo.record_order(
            request,
            OrderResult(client_order_id=cid, status=status, filled_qty=d("0.1")),
            mode="paper",
            created_at=at,
        )

    def test_six_orders_per_minute_allowed_seventh_blocked(self):
        for index in range(5):
            self.record(f"SYM{index}", "BUY", self.now - timedelta(seconds=index + 1))
        self.assertTrue(self.guard.check("NEW", "BUY", self.now).allowed)
        self.record("SYM5", "BUY", self.now - timedelta(seconds=1))
        decision = self.guard.check("NEW", "BUY", self.now)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "order-rate-limit")

    def test_duplicate_same_symbol_and_side_blocked(self):
        self.record("NVDA", "BUY", self.now - timedelta(seconds=10))
        decision = self.guard.check("NVDA", "BUY", self.now)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "duplicate-order")

    def test_opposite_side_allowed(self):
        self.record("NVDA", "BUY", self.now - timedelta(seconds=10))
        self.assertTrue(self.guard.check("NVDA", "SELL", self.now).allowed)

    def test_rejected_order_does_not_block(self):
        self.record("NVDA", "BUY", self.now - timedelta(seconds=10), status="REJECTED")
        self.assertTrue(self.guard.check("NVDA", "BUY", self.now).allowed)


class DailyRiskGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(Database(Path(self.tmp.name) / "state.db"))
        self.guard = DailyRiskGuard(self.repo, load_guards(ROOT / "config" / "guards.yaml").risk)
        self.today = date(2026, 12, 1)
        self.now = et(2026, 12, 1, 10, 30)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_five_percent_loss_halts_buys_for_the_day(self):
        self.repo.ensure_day_state(self.today, d("400"), self.now)
        self.assertTrue(self.guard.check(self.today, d("390"), self.now).allowed)
        decision = self.guard.check(self.today, d("380"), self.now)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "daily-loss-limit")
        self.assertEqual(self.repo.day_state(self.today).buys_halted_reason, "daily-loss-limit")
        # 이후 회복해도 그날은 계속 정지
        self.assertFalse(self.guard.check(self.today, d("395"), self.now).allowed)


class ReconcileTests(unittest.TestCase):
    def position(self, symbol="NVDA", qty="0.5"):
        return Position(symbol, d(qty), d("100"), et(2026, 12, 1, 10), date(2026, 12, 1), d("8"))

    def test_match(self):
        result = reconcile([self.position()], {"NVDA": PositionView("NVDA", d("0.5"), d("100"))})
        self.assertTrue(result.ok)

    def test_local_only(self):
        result = reconcile([self.position()], {})
        self.assertFalse(result.ok)
        self.assertIn("로컬에만", result.mismatches[0])

    def test_broker_only(self):
        result = reconcile([], {"NVDA": PositionView("NVDA", d("0.5"), d("100"))})
        self.assertFalse(result.ok)
        self.assertIn("브로커에만", result.mismatches[0])

    def test_tolerance(self):
        result = reconcile(
            [self.position(qty="0.5")], {"NVDA": PositionView("NVDA", d("0.5000004"), d("100"))}
        )
        self.assertTrue(result.ok)

    def test_quantity_difference(self):
        result = reconcile([self.position(qty="0.5")], {"NVDA": PositionView("NVDA", d("0.4"), d("100"))})
        self.assertFalse(result.ok)
        self.assertEqual(result.details["NVDA"], {"local": "0.5", "broker": "0.4"})


if __name__ == "__main__":
    unittest.main()
