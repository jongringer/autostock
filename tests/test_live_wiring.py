"""live 스택 통합 테스트 — 토스 클라이언트/브로커/캘린더/시세 + 엔진을 가짜 HTTP 로 연결.

실제 토스 서버에는 접속하지 않는다.
"""

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from helpers import FakeHTTP, ROOT, d, et, falling_closes, make_daily, json_response

from alpha_shield.broker.toss import TossBroker
from alpha_shield.cache import TTLCache
from alpha_shield.calendar import TossUsCalendar
from alpha_shield.clock import FixedClock
from alpha_shield.config import load_guards, load_strategy
from alpha_shield.engine.alerts import Alerter, DecisionLog
from alpha_shield.engine.loop import Engine
from alpha_shield.marketdata import CompositeMarketData, TossClient, TossMarketData
from alpha_shield.marketdata.macro import MacroCalendar
from alpha_shield.models import Bar
from alpha_shield.safety import SafetyLock
from alpha_shield.state.db import Database
from alpha_shield.state.repo import Repo


class IndicesStub:
    def futures_change_pct(self, symbol):
        return d("0.4")

    def last_daily_close(self, symbol):
        return d("15.5"), et(2026, 11, 30, 16)

    def last_price(self, symbol):
        return d("15.5")


def candle_rows(bars, limit=200):
    return [
        {
            "timestamp": bar.timestamp.isoformat(),
            "openPrice": str(bar.open),
            "highPrice": str(bar.high),
            "lowPrice": str(bar.low),
            "closePrice": str(bar.close),
            "volume": "1000",
            "currency": "USD",
        }
        for bar in reversed(bars[-limit:])
    ]


class LiveWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.now = et(2026, 12, 1, 10, 30)
        self.clock = FixedClock(self.now)
        self.http = FakeHTTP()
        self.price = d("96")
        self.price_ts = "2026-12-02T00:29:30+09:00"
        self.last_order_body = None
        self.order_seq = 0

        self.http.on(
            "/oauth2/token",
            json_response({"access_token": "tok", "token_type": "Bearer", "expires_in": 86400}),
            method="POST",
        )
        self.http.on(
            "/api/v1/accounts",
            json_response({"result": [{"accountNo": "1", "accountSeq": 7, "accountType": "BROKERAGE"}]}),
        )
        self.http.on(
            "/api/v1/market-calendar/US",
            json_response(
                {
                    "result": {
                        "today": {
                            "date": "2026-12-01",
                            "regularMarket": {
                                "startTime": "2026-12-01T23:30:00+09:00",
                                "endTime": "2026-12-02T06:00:00+09:00",
                            },
                        },
                        "previousBusinessDay": {"date": "2026-11-30"},
                        "nextBusinessDay": {"date": "2026-12-02"},
                    }
                }
            ),
        )

        self.daily = make_daily(falling_closes(), last_day=date(2026, 11, 30))
        self.intraday = make_daily([96] * 30, last_day=date(2026, 12, 1), spread="0.005")
        last = self.intraday[-1]
        self.intraday[-1] = Bar(timestamp=last.timestamp, open=d("100"), high=d("100.5"), low=d("95.5"), close=d("96"))

        def candles_responder(method, url):
            params = self.http.find("/api/v1/candles").params or {}
            rows = candle_rows(self.daily if params.get("interval") == "1d" else self.intraday)
            return json_response({"result": {"candles": rows, "nextBefore": None}})

        self.http.route(lambda m, u, p: "/api/v1/candles" in u, candles_responder)

        def prices_responder(method, url):
            return json_response(
                {
                    "result": [
                        {"symbol": "NVDA", "timestamp": self.price_ts, "lastPrice": str(self.price), "currency": "USD"}
                    ]
                }
            )

        self.http.route(lambda m, u, p: "/api/v1/prices" in u, prices_responder)
        self.http.on(
            "/api/v1/buying-power",
            json_response({"result": {"currency": "USD", "cashBuyingPower": "380.00"}}),
        )
        self.http.on("/api/v1/holdings", json_response({"result": {"items": []}}))

        def post_order(method, url):
            self.order_seq += 1
            self.last_order_body = self.http.find("/api/v1/orders", method="POST").json_body
            return json_response({"result": {"orderId": f"OID-{self.order_seq}", "clientOrderId": "x"}})

        def get_order(method, url):
            body = self.last_order_body or {}
            side = body.get("side", "BUY")
            quantity = body.get("quantity") or "0.885416"
            amount = body.get("orderAmount") or str((d(quantity) * self.price).quantize(d("0.01")))
            return json_response(
                {
                    "result": {
                        "orderId": f"OID-{self.order_seq}",
                        "symbol": body.get("symbol", "NVDA"),
                        "side": side,
                        "orderType": "MARKET",
                        "timeInForce": "DAY",
                        "status": "FILLED",
                        "price": None,
                        "quantity": quantity,
                        "orderAmount": amount if side == "BUY" else None,
                        "currency": "USD",
                        "orderedAt": "2026-12-02T00:30:05+09:00",
                        "canceledAt": None,
                        "execution": {
                            "filledQuantity": quantity,
                            "averageFilledPrice": str(self.price),
                            "filledAmount": amount,
                            "commission": "0.09",
                            "tax": "0",
                            "filledAt": "2026-12-02T00:30:05+09:00",
                            "settlementDate": "2026-12-03",
                        },
                    }
                }
            )

        self.http.route(lambda m, u, p: m == "POST" and u.endswith("/api/v1/orders"), post_order)
        self.http.route(lambda m, u, p: m == "GET" and "/api/v1/orders/" in u, get_order)

        self.strategy = load_strategy(ROOT / "config" / "strategy.yaml")
        self.guards = load_guards(ROOT / "config" / "guards.yaml")
        self.repo = Repo(Database(self.base / "state.db"))
        self.client = TossClient(
            "cid",
            "secret",
            self.base / "token.json",
            transport=self.http,
            clock=lambda: 1_800_000_000.0,
            sleep=lambda _s: None,
        )
        self.calendar = TossUsCalendar(self.client)
        self.market_data = CompositeMarketData(
            TossMarketData(self.client, cache=TTLCache(clock=lambda: self.clock.now().timestamp())), IndicesStub()
        )
        armed = self.base / "ARMED"
        armed.touch()
        self.broker = TossBroker(
            self.client,
            SafetyLock(True, armed),
            min_order_usd=self.strategy.capital.min_order_usd,
            session_provider=lambda now: self.calendar.session_on(now.date()),
            fractional_close_buffer_min=self.guards.session.fractional_close_buffer_min,
            clock=self.clock.now,
            sleep=lambda _s: None,
        )
        self.engine = Engine(
            mode="live",
            strategy=self.strategy,
            guards_config=self.guards,
            repo=self.repo,
            broker=self.broker,
            market_data=self.market_data,
            calendar=self.calendar,
            clock=self.clock,
            halt_file=self.base / "halt.flag",
            alerter=Alerter(self.base / "alerts.jsonl", clock=self.clock.now),
            decision_log=DecisionLog(self.base / "decisions.jsonl"),
            macro_calendar=MacroCalendar(ROOT / "config" / "macro_calendar_2026.json", d("0.5")),
        )

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_live_stack_places_amount_order(self):
        report = self.engine.run_cycle()
        self.assertEqual(len(report.buys), 1)
        self.assertEqual(report.buys[0].status, "FILLED")
        order_call = self.http.find("/api/v1/orders", method="POST")
        self.assertEqual(order_call.json_body["orderAmount"], "85.00")
        self.assertEqual(order_call.headers["X-Tossinvest-Account"], "7")
        position = self.repo.position("NVDA")
        self.assertEqual(position.qty, d("0.885416"))
        self.assertEqual(position.avg_price, d("96.00"))
        self.assertEqual(position.tp_target_pct, d("8.0"))
        window = self.calendar.session_on(date(2026, 12, 1))
        self.assertEqual(window.regular_start.hour, 9)
        self.assertEqual(window.regular_end.hour, 16)

    def test_live_sell_after_take_profit(self):
        self.engine.run_cycle()
        self.clock.advance(300)
        self.price = d("103.68")
        self.price_ts = "2026-12-02T00:34:30+09:00"
        report = self.engine.run_cycle()
        self.assertEqual(len(report.sells), 1)
        self.assertEqual(report.sells[0].status, "FILLED")
        self.assertIsNone(self.repo.position("NVDA"))
        sell_call = self.http.all("/api/v1/orders", "POST")[-1]
        self.assertEqual(sell_call.json_body["side"], "SELL")
        self.assertEqual(sell_call.json_body["quantity"], "0.885416")


if __name__ == "__main__":
    unittest.main()
