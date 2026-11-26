import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from helpers import FakeHTTP, et, json_response

from alpha_shield.marketdata.toss import (
    InsufficientBuyingPower,
    TossApiError,
    TossAuthError,
    TossClient,
    TossMarketData,
)


class TossClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.token_path = Path(self.tmp.name) / "toss_token.json"
        self.http = FakeHTTP()
        self.now = 1_800_000_000.0
        self.tokens = {"n": 0}

        def token_responder(method, url):
            self.tokens["n"] += 1
            return json_response(
                {"access_token": f"tok-{self.tokens['n']}", "token_type": "Bearer", "expires_in": 86400}
            )

        self.http.on("/oauth2/token", token_responder, method="POST")
        self.http.on(
            "/api/v1/accounts",
            json_response({"result": [{"accountNo": "12345678901", "accountSeq": 7, "accountType": "BROKERAGE"}]}),
        )
        self.client = TossClient(
            "cid",
            "secret",
            self.token_path,
            transport=self.http,
            clock=lambda: self.now,
            sleep=lambda _s: None,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_token_request_form_and_reuse(self):
        token = self.client.access_token()
        self.assertEqual(token, "tok-1")
        self.client.access_token()
        token_calls = self.http.all("/oauth2/token", "POST")
        self.assertEqual(len(token_calls), 1)
        form = token_calls[0].form
        self.assertEqual(form["grant_type"], "client_credentials")
        self.assertEqual(form["client_id"], "cid")
        self.assertEqual(form["client_secret"], "secret")

    def test_token_file_mode_0600(self):
        self.client.access_token()
        mode = stat.S_IMODE(os.stat(self.token_path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_token_reissued_after_expiry_margin(self):
        self.client.access_token()
        self.now += 86400 - 300  # 만료 10분 전 구간 진입
        self.assertEqual(self.client.access_token(), "tok-2")
        self.assertEqual(len(self.http.all("/oauth2/token", "POST")), 2)

    def test_saved_token_is_reused_by_new_client(self):
        self.client.access_token()
        other = TossClient("cid", "secret", self.token_path, transport=self.http, clock=lambda: self.now, sleep=lambda _s: None)
        self.assertEqual(other.access_token(), "tok-1")
        self.assertEqual(len(self.http.all("/oauth2/token", "POST")), 1)

    def test_different_client_ignores_saved_token(self):
        self.client.access_token()
        other = TossClient("cid-2", "secret", self.token_path, transport=self.http, clock=lambda: self.now, sleep=lambda _s: None)
        other.access_token()
        self.assertEqual(len(self.http.all("/oauth2/token", "POST")), 2)

    def test_auth_failure_raises(self):
        http = FakeHTTP()
        http.on("/oauth2/token", json_response({"error": "access_denied", "error_description": "ip"}, status=403))
        client = TossClient("cid", "secret", Path(self.tmp.name) / "t2.json", transport=http, clock=lambda: self.now)
        with self.assertRaises(TossAuthError) as ctx:
            client.access_token()
        self.assertEqual(ctx.exception.status, 403)

    def test_account_header_on_account_scoped_calls(self):
        self.http.on("/api/v1/holdings", json_response({"result": {"items": []}}))
        self.client.get("/api/v1/holdings", needs_account=True)
        call = self.http.find("/api/v1/holdings")
        self.assertEqual(call.headers["X-Tossinvest-Account"], "7")
        self.assertTrue(call.headers["Authorization"].startswith("Bearer "))

    def test_prices_parse(self):
        self.http.on(
            "/api/v1/prices",
            json_response(
                {
                    "result": [
                        {"symbol": "NVDA", "timestamp": "2026-12-01T23:30:00.123+09:00", "lastPrice": "185.70", "currency": "USD"}
                    ]
                }
            ),
        )
        quotes = TossMarketData(self.client).quotes(["NVDA"])
        self.assertEqual(quotes["NVDA"].last_price, __import__("decimal").Decimal("185.70"))
        self.assertEqual(quotes["NVDA"].timestamp.astimezone(__import__("alpha_shield.clock", fromlist=["ET"]).ET).hour, 9)
        call = self.http.find("/api/v1/prices")
        self.assertEqual(call.params["symbols"], "NVDA")

    def test_candles_paginate_and_sort(self):
        page1 = {
            "result": {
                "candles": [
                    {"timestamp": "2026-12-01T00:00:00+09:00", "openPrice": "10", "highPrice": "11", "lowPrice": "9", "closePrice": "10", "volume": "1", "currency": "USD"},
                    {"timestamp": "2026-11-30T00:00:00+09:00", "openPrice": "9", "highPrice": "10", "lowPrice": "8", "closePrice": "9", "volume": "1", "currency": "USD"},
                ],
                "nextBefore": "2026-11-30T00:00:00+09:00",
            }
        }
        page2 = {
            "result": {
                "candles": [
                    {"timestamp": "2026-11-29T00:00:00+09:00", "openPrice": "8", "highPrice": "9", "lowPrice": "7", "closePrice": "8", "volume": "1", "currency": "USD"}
                ],
                "nextBefore": None,
            }
        }
        counter = {"n": 0}

        def responder(method, url):
            counter["n"] += 1
            return json_response(page1 if counter["n"] == 1 else page2)

        self.http.on("/api/v1/candles", responder)
        bars = TossMarketData(self.client).daily_bars("NVDA", count=250)
        self.assertEqual(len(bars), 3)
        self.assertEqual(bars[0].close, __import__("decimal").Decimal("8"))
        self.assertEqual(bars[-1].close, __import__("decimal").Decimal("10"))
        self.assertEqual(len(self.http.all("/api/v1/candles")), 2)

    def test_401_token_revoked_triggers_one_reissue(self):
        state = {"price_calls": 0}

        def prices(method, url):
            state["price_calls"] += 1
            if state["price_calls"] == 1:
                return json_response({"error": {"code": "token-revoked", "message": "revoked"}}, status=401)
            return json_response({"result": []})

        self.http.on("/api/v1/prices", prices)
        result = self.client.get("/api/v1/prices", params={"symbols": "NVDA"})
        self.assertEqual(result, [])
        self.assertEqual(state["price_calls"], 2)
        self.assertEqual(len(self.http.all("/oauth2/token", "POST")), 2)

    def test_error_envelope_maps_insufficient_buying_power(self):
        self.http.on(
            "/api/v1/orders",
            json_response({"error": {"code": "insufficient-buying-power", "message": "부족", "requestId": "R1"}}, status=422),
        )
        with self.assertRaises(InsufficientBuyingPower) as ctx:
            self.client.post("/api/v1/orders", json_body={"symbol": "NVDA"}, needs_account=True)
        self.assertEqual(ctx.exception.status, 422)
        self.assertEqual(ctx.exception.request_id, "R1")

    def test_rate_limit_error_carries_retryable(self):
        self.http.on("/api/v1/prices", json_response({"error": {"code": "rate-limit-exceeded"}}, status=429))
        with self.assertRaises(TossApiError) as ctx:
            self.client.get("/api/v1/prices")
        self.assertTrue(ctx.exception.retryable)


if __name__ == "__main__":
    unittest.main()
