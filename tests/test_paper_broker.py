import tempfile
import unittest
from pathlib import Path

from helpers import d, et

from alpha_shield.broker.base import OrderRejected
from alpha_shield.broker.paper import JsonFileSnapshotStore, PaperBroker
from alpha_shield.models import ORDER_STATUS_FILLED, ORDER_STATUS_REJECTED, OrderRequest


class PaperBrokerTests(unittest.TestCase):
    def broker(self, cash="380", slippage="0", commission="0"):
        return PaperBroker(d(cash), slippage_bps=d(slippage), commission_bps=d(commission), clock=lambda: et(2026, 12, 1, 10, 30))

    def buy(self, broker, symbol="NVDA", amount="85", price="100", cid=None):
        return broker.place_order(
            OrderRequest(symbol, "BUY", cid or f"as-20261201-{symbol}-e001", amount_usd=d(amount), ref_price=d(price))
        )

    def sell(self, broker, symbol="NVDA", qty="0.85", price="100", cid=None):
        return broker.place_order(
            OrderRequest(symbol, "SELL", cid or f"as-20261201-{symbol}-s001", qty=d(qty), ref_price=d(price))
        )

    def test_buy_by_amount(self):
        broker = self.broker()
        result = self.buy(broker)
        self.assertEqual(result.status, ORDER_STATUS_FILLED)
        self.assertEqual(result.filled_qty, d("0.85"))
        self.assertEqual(result.avg_price, d("100.00"))
        account = broker.account()
        self.assertEqual(account.cash_usd, d("295"))
        self.assertEqual(account.positions["NVDA"].qty, d("0.85"))

    def test_commission_and_slippage(self):
        broker = PaperBroker(d("380"), slippage_bps=d("10"), commission_bps=d("10"), clock=lambda: et(2026, 12, 1, 10, 30))
        result = self.buy(broker, amount="85", price="100")
        # 체결가 100.1, 수량 85/100.1 = 0.849150, 금액 85.00, 수수료 0.085
        self.assertEqual(result.avg_price, d("100.10"))
        self.assertEqual(result.filled_qty, d("0.849150"))
        self.assertEqual(result.commission, d("0.08"))
        self.assertLess(broker.account().cash_usd, d("295"))

    def test_sell_full_position_closes_it(self):
        broker = self.broker()
        self.buy(broker)
        result = self.sell(broker, qty="0.85", price="110")
        self.assertEqual(result.status, ORDER_STATUS_FILLED)
        self.assertNotIn("NVDA", broker.account().positions)
        self.assertEqual(broker.account().cash_usd, d("388.5"))

    def test_partial_sell_keeps_average(self):
        broker = self.broker()
        self.buy(broker)
        result = self.sell(broker, qty="0.25", price="100")
        self.assertEqual(result.filled_qty, d("0.25"))
        view = broker.account().positions["NVDA"]
        self.assertEqual(view.qty, d("0.6"))
        self.assertEqual(view.avg_price, d("100"))

    def test_second_buy_weights_average(self):
        broker = self.broker(cash="500")
        self.buy(broker, amount="50", price="100")
        self.buy(broker, amount="50", price="50", cid="as-20261201-NVDA-a001")
        view = broker.account().positions["NVDA"]
        self.assertEqual(view.qty, d("1.5"))
        self.assertEqual(view.avg_price.quantize(d("0.000001")), d("66.666667"))

    def test_insufficient_cash_rejected(self):
        broker = self.broker(cash="80")
        result = self.buy(broker, amount="85")
        self.assertEqual(result.status, ORDER_STATUS_REJECTED)
        self.assertEqual(result.error_code, "insufficient-buying-power")
        self.assertEqual(broker.account().cash_usd, d("80"))

    def test_sell_without_position_rejected(self):
        broker = self.broker()
        result = self.sell(broker, qty="1")
        self.assertEqual(result.status, ORDER_STATUS_REJECTED)
        self.assertEqual(result.error_code, "insufficient-sellable-quantity")

    def test_missing_reference_price_raises(self):
        broker = self.broker()
        with self.assertRaises(OrderRejected):
            broker.place_order(OrderRequest("NVDA", "BUY", "as-1", amount_usd=d("20")))

    def test_snapshot_restore_via_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonFileSnapshotStore(Path(tmp) / "paper.json")
            broker = PaperBroker(d("380"), slippage_bps=d("0"), commission_bps=d("0"), store=store, clock=lambda: et(2026, 12, 1, 10, 30))
            self.buy(broker)
            restored = PaperBroker(d("0"), store=store, clock=lambda: et(2026, 12, 1, 10, 30))
            account = restored.account()
            self.assertEqual(account.cash_usd, d("295"))
            self.assertEqual(account.positions["NVDA"].qty, d("0.85"))

    def test_update_prices_changes_equity(self):
        broker = self.broker()
        self.buy(broker)
        broker.update_prices({"NVDA": d("120")})
        account = broker.account()
        self.assertEqual(account.equity_usd, d("295") + d("0.85") * d("120"))


if __name__ == "__main__":
    unittest.main()
