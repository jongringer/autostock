import csv
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from helpers import d, et

from alpha_shield.models import ORDER_STATUS_FILLED, OrderRequest, OrderResult, Position
from alpha_shield.reporting.ledger import export_ledger
from alpha_shield.state.db import Database
from alpha_shield.state.repo import Repo


class RepoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(Database(Path(self.tmp.name) / "state.db"))
        self.now = et(2026, 12, 1, 10, 30)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_position_roundtrip_keeps_decimals(self):
        position = Position(
            symbol="NVDA",
            qty=d("0.4721"),
            avg_price=d("101.23"),
            entry_at=self.now,
            first_entry_date=date(2026, 12, 1),
            tp_target_pct=d("8"),
            entry_vix=d("15.5"),
            add_count=0,
        )
        self.repo.save_position(position)
        loaded = self.repo.position("NVDA")
        self.assertEqual(loaded, position)
        self.assertEqual(loaded.qty, d("0.4721"))

    def test_position_delete(self):
        self.repo.save_position(
            Position("TSLA", d("0.1"), d("200"), self.now, date(2026, 12, 1), d("5"))
        )
        self.repo.delete_position("TSLA")
        self.assertIsNone(self.repo.position("TSLA"))

    def test_cooldown(self):
        self.repo.set_cooldown("NVDA", self.now + timedelta(seconds=900), "stop_loss")
        self.assertEqual(self.repo.cooldown_until("NVDA"), self.now + timedelta(seconds=900))
        self.repo.clear_cooldown("NVDA")
        self.assertIsNone(self.repo.cooldown_until("NVDA"))

    def test_signal_status_flow(self):
        signal_id = self.repo.add_signal("NVDA", self.now, self.now + timedelta(seconds=900), d("3"), d("1.04"))
        self.assertEqual(len(self.repo.signals("PENDING")), 1)
        self.repo.set_signal_status(signal_id, "CONSUMED")
        self.assertEqual(self.repo.signals("PENDING"), [])
        self.assertEqual(len(self.repo.signals("CONSUMED")), 1)

    def test_order_record_and_lookup(self):
        request = OrderRequest("NVDA", "BUY", "as-20261201-NVDA-e001", amount_usd=d("85.00"), reason="entry")
        result = OrderResult(
            client_order_id=request.client_order_id,
            status=ORDER_STATUS_FILLED,
            broker_order_id="OID-1",
            filled_qty=d("0.85"),
            avg_price=d("100"),
            filled_amount=d("85"),
            commission=d("0.09"),
            filled_at=self.now,
        )
        self.repo.record_order(request, result, mode="paper", created_at=self.now)
        record = self.repo.order_by_client_id(request.client_order_id)
        self.assertEqual(record.status, ORDER_STATUS_FILLED)
        self.assertEqual(record.filled_qty, d("0.85"))
        self.assertEqual(record.avg_price, d("100"))
        self.assertEqual(len(self.repo.filled_orders()), 1)

    def test_count_orders_filters(self):
        for index in range(3):
            request = OrderRequest("NVDA", "BUY", f"id-{index}", amount_usd=d("20"))
            self.repo.record_order(
                request,
                OrderResult(client_order_id=f"id-{index}", status="REJECTED"),
                mode="paper",
                created_at=self.now - timedelta(seconds=index * 10),
            )
        self.assertEqual(self.repo.count_orders(self.now - timedelta(seconds=60)), 3)
        self.assertEqual(self.repo.count_orders(self.now - timedelta(seconds=60), symbol="NVDA", side="BUY"), 3)
        self.assertEqual(self.repo.count_orders(self.now - timedelta(seconds=60), symbol="TSLA"), 0)

    def test_day_state_and_daily_pnl(self):
        state = self.repo.ensure_day_state(date(2026, 12, 1), d("380"), self.now)
        self.assertEqual(state.equity_open, d("380"))
        self.repo.halt_buys(date(2026, 12, 1), "daily-loss-limit", self.now)
        self.assertEqual(self.repo.day_state(date(2026, 12, 1)).buys_halted_reason, "daily-loss-limit")
        self.repo.add_realized_pnl(date(2026, 12, 1), d("-1.5"), self.now)
        self.assertEqual(self.repo.day_state(date(2026, 12, 1)).realized_pnl, d("-1.5"))

    def test_ledger_export_csv(self):
        request = OrderRequest("NVDA", "BUY", "as-20261201-NVDA-e001", amount_usd=d("85.00"), reason="entry")
        result = OrderResult(
            client_order_id=request.client_order_id,
            status=ORDER_STATUS_FILLED,
            broker_order_id="OID-1",
            filled_qty=d("0.85"),
            avg_price=d("100"),
            filled_amount=d("85"),
            commission=d("0.09"),
            filled_at=self.now,
        )
        self.repo.record_order(request, result, mode="paper", created_at=self.now)
        path = export_ledger(self.repo, Path(self.tmp.name) / "ledger", "2026-12")
        self.assertTrue(path.exists())
        with path.open(encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "NVDA")
        self.assertEqual(rows[0]["quantity"], "0.85")
        self.assertEqual(rows[0]["filled_at_kst"][:10], "2026-12-02")  # KST 는 ET+14h


if __name__ == "__main__":
    unittest.main()
