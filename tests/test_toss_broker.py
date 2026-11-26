import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from helpers import FakeHTTP, ROOT, d, et, json_response

from alpha_shield.broker.toss import TossBroker
from alpha_shield.calendar import StaticUsCalendar
from alpha_shield.config import load_guards, load_strategy
from alpha_shield.marketdata.toss import TossClient
from alpha_shield.models import ORDER_STATUS_FILLED, ORDER_STATUS_PARTIAL, ORDER_STATUS_REJECTED, OrderRequest
from alpha_shield.safety import LiveOrdersDisabled, LiveOrdersEnabledButNotArmed, SafetyLock


class TossBrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.http = FakeHTTP()
        self.http.on(
            "/oauth2/token",
            json_response({"access_token": "tok", "token_type": "Bearer", "expires_in": 86400}),
            method="POST",
        )
        self.http.on(
            "/api/v1/accounts",
            json_response({"result": [{"accountNo": "1", "accountSeq": 7, "accountType": "BROKERAGE"}]}),
        )
        self.now = et(2026, 12, 1, 10, 30)
        self.client = TossClient(
            "cid",
            "secret",
            Path(self.tmp.name) / "token.json",
            transport=self.http,
            clock=lambda: 1_800_000_000.0,
            sleep=lambda _s: None,
        )
        self.armed = Path(self.tmp.name) / "ARMED"
        self.armed.touch()
        self.calendar = StaticUsCalendar(ROOT / "config" / "market_calendar_2026.json")
        strategy = load_strategy(ROOT / "config" / "strategy.yaml")
        guards = load_guards(ROOT / "config" / "guards.yaml")
        self.broker = TossBroker(
            self.client,
            SafetyLock(True, self.armed),
            min_order_usd=strategy.capital.min_order_usd,
            session_provider=lambda now: self.calendar.session_on(now.date()),
            fractional_close_buffer_min=guards.session.fractional_close_buffer_min,
            clock=lambda: self.now,
            sleep=lambda _s: None,
            fill_poll_attempts=1,
        )
        self._order_detail = self._filled_detail()

    def tearDown(self):
        self.tmp.cleanup()

    def _filled_detail(self, status="FILLED", filled="0.845", avg="100.60", amount="85.00"):
        return json_response(
            {
                "result": {
                    "orderId": "OID-1",
                    "symbol": "NVDA",
                    "side": "BUY",
                    "orderType": "MARKET",
                    "timeInForce": "DAY",
                    "status": status,
                    "price": None,
                    "quantity": filled,
                    "orderAmount": amount,
                    "currency": "USD",
                    "orderedAt": "2026-12-01T23:30:00+09:00",
                    "canceledAt": None,
                    "execution": {
                        "filledQuantity": filled,
                        "averageFilledPrice": avg,
                        "filledAmount": amount,
                        "commission": "0.08",
                        "tax": "0",
                        "filledAt": "2026-12-01T23:30:05+09:00",
                        "settlementDate": "2026-12-03",
                    },
                }
            }
        )

    def _wire_order(self, status="FILLED", filled="0.845", avg="100.60", amount="85.00"):
        self.http.route(
            lambda method, url, params: method == "POST" and url.endswith("/api/v1/orders"),
            lambda method, url: json_response({"result": {"orderId": "OID-1", "clientOrderId": "as-20261201-NVDA-e001"}}),
        )
        self.http.route(
            lambda method, url, params: method == "GET" and "/api/v1/orders/" in url,
            lambda method, url: self._filled_detail(status, filled, avg, amount),
        )

    def test_buy_sends_order_amount_and_account_header(self):
        self._wire_order()
        result = self.broker.place_order(
            OrderRequest("NVDA", "BUY", "as-20261201-NVDA-e001", amount_usd=d("85"), ref_price=d("100"))
        )
        self.assertEqual(result.status, ORDER_STATUS_FILLED)
        self.assertEqual(result.filled_qty, d("0.845"))
        call = self.http.find("/api/v1/orders", method="POST")
        self.assertEqual(call.headers["X-Tossinvest-Account"], "7")
        self.assertEqual(call.json_body["symbol"], "NVDA")
        self.assertEqual(call.json_body["side"], "BUY")
        self.assertEqual(call.json_body["orderType"], "MARKET")
        self.assertEqual(call.json_body["orderAmount"], "85.00")
        self.assertEqual(call.json_body["clientOrderId"], "as-20261201-NVDA-e001")
        self.assertNotIn("quantity", call.json_body)

    def test_sell_sends_fractional_quantity_six_decimals(self):
        self._wire_order()
        result = self.broker.place_order(
            OrderRequest("NVDA", "SELL", "as-20261201-NVDA-x001", qty=d("0.8456789"), ref_price=d("100"))
        )
        self.assertEqual(result.status, ORDER_STATUS_FILLED)
        call = self.http.find("/api/v1/orders", method="POST")
        self.assertEqual(call.json_body["side"], "SELL")
        self.assertEqual(call.json_body["quantity"], "0.845678")
        self.assertNotIn("orderAmount", call.json_body)

    def test_amount_rounded_down_to_two_decimals(self):
        self._wire_order()
        self.broker.place_order(OrderRequest("NVDA", "BUY", "as-1", amount_usd=d("85.999"), ref_price=d("100")))
        self.assertEqual(self.http.find("/api/v1/orders", method="POST").json_body["orderAmount"], "85.99")

    def test_min_order_rejected(self):
        result = self.broker.place_order(OrderRequest("NVDA", "BUY", "as-1", amount_usd=d("5")))
        self.assertEqual(result.status, ORDER_STATUS_REJECTED)
        self.assertEqual(result.error_code, "min-order-amount")
        self.assertIsNone(self.http.find("/api/v1/orders", method="POST"))

    def test_outside_fractional_window_rejected(self):
        self.now = et(2026, 12, 1, 15, 30)
        result = self.broker.place_order(OrderRequest("NVDA", "SELL", "as-1", qty=d("1")))
        self.assertEqual(result.status, ORDER_STATUS_REJECTED)
        self.assertEqual(result.error_code, "amount-order-outside-regular-hours")

    def test_live_lock_requires_both_conditions(self):
        broker = TossBroker(self.client, SafetyLock(False, self.armed), clock=lambda: self.now, sleep=lambda _s: None)
        with self.assertRaises(LiveOrdersDisabled):
            broker.place_order(OrderRequest("NVDA", "BUY", "as-1", amount_usd=d("20")))

        self.armed.unlink()
        broker2 = TossBroker(self.client, SafetyLock(True, self.armed), clock=lambda: self.now, sleep=lambda _s: None)
        with self.assertRaises(LiveOrdersEnabledButNotArmed):
            broker2.place_order(OrderRequest("NVDA", "BUY", "as-1", amount_usd=d("20")))

    def test_request_in_progress_retries_same_key(self):
        state = {"posts": 0}

        def order_route(method, url):
            state["posts"] += 1
            if state["posts"] == 1:
                return json_response({"error": {"code": "request-in-progress"}}, status=409)
            return json_response({"result": {"orderId": "OID-1"}})

        self.http.route(lambda m, u, p: m == "POST" and u.endswith("/api/v1/orders"), order_route)
        self.http.route(
            lambda m, u, p: m == "GET" and "/api/v1/orders/" in u,
            lambda m, u: self._filled_detail(),
        )
        result = self.broker.place_order(OrderRequest("NVDA", "BUY", "as-1", amount_usd=d("85")))
        self.assertEqual(result.status, ORDER_STATUS_FILLED)
        self.assertEqual(state["posts"], 2)

    def test_insufficient_buying_power_returns_rejected(self):
        self.http.route(
            lambda m, u, p: m == "POST" and u.endswith("/api/v1/orders"),
            lambda m, u: json_response({"error": {"code": "insufficient-buying-power", "message": "부족"}}, status=422),
        )
        result = self.broker.place_order(OrderRequest("NVDA", "BUY", "as-1", amount_usd=d("85")))
        self.assertEqual(result.status, ORDER_STATUS_REJECTED)
        self.assertEqual(result.error_code, "insufficient-buying-power")

    def test_partial_fill_mapped(self):
        self._wire_order(status="PARTIAL_FILLED", filled="0.5", avg="100.5", amount="50.25")
        result = self.broker.place_order(OrderRequest("NVDA", "BUY", "as-1", amount_usd=d("85")))
        self.assertEqual(result.status, ORDER_STATUS_PARTIAL)
        self.assertEqual(result.filled_qty, d("0.5"))
        self.assertEqual(result.commission, d("0.08"))

    def test_account_reads_buying_power_and_us_holdings(self):
        self.http.on(
            "/api/v1/buying-power",
            json_response({"result": {"currency": "USD", "cashBuyingPower": "210.55"}}),
        )
        self.http.on(
            "/api/v1/holdings",
            json_response(
                {
                    "result": {
                        "items": [
                            {
                                "symbol": "NVDA",
                                "name": "NVIDIA",
                                "marketCountry": "US",
                                "currency": "USD",
                                "quantity": "0.845",
                                "lastPrice": "101.0",
                                "averagePurchasePrice": "100.6",
                            },
                            {
                                "symbol": "005930",
                                "name": "삼성전자",
                                "marketCountry": "KR",
                                "currency": "KRW",
                                "quantity": "10",
                                "lastPrice": "70000",
                                "averagePurchasePrice": "65000",
                            },
                        ]
                    }
                }
            ),
        )
        account = self.broker.account()
        self.assertEqual(account.cash_usd, d("210.55"))
        self.assertEqual(list(account.positions), ["NVDA"])
        self.assertEqual(account.positions["NVDA"].qty, d("0.845"))
        self.assertAlmostEqual(float(account.equity_usd), float(d("210.55") + d("0.845") * d("101.0")), places=4)
        self.assertIn("/api/v1/holdings", self.http.find("/api/v1/holdings").url)
        self.assertEqual(self.http.find("/api/v1/buying-power").params["currency"], "USD")


if __name__ == "__main__":
    unittest.main()
