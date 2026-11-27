"""내장 웹 대시보드(`alpha_shield.web`) API 테스트.

실제 네트워크 없이 InMemoryMarketData 로 시세를 대신하고, 엔진은 스텁으로 바꾼다.
표준 라이브러리 http.client 로 진짜 HTTP 요청을 보내 라우팅·토큰·상태 코드를 검증한다.
"""

from __future__ import annotations

import http.client
import json
import pathlib
import threading
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urlparse

import pytest

from alpha_shield.calendar import StaticUsCalendar
from alpha_shield.cli import build_parser
from alpha_shield.clock import ET
from alpha_shield.config import load_all, load_settings
from alpha_shield.marketdata.base import InMemoryMarketData
from alpha_shield.models import Bar, CycleReport, OrderRequest, OrderResult, Position, Quote
from alpha_shield.state.db import Database
from alpha_shield.state.repo import Repo
from alpha_shield.web import WebApp, create_server

from helpers import make_daily, make_intraday

ROOT = pathlib.Path(__file__).resolve().parents[1]
UNIVERSE = ["NVDA", "TSLA", "AMZN", "TSM", "AVGO", "QQQ", "AAPL", "GOOGL"]
TOKEN = "test-token"


def dec(value: object) -> Decimal:
    return Decimal(str(value))


def flat_daily(close: object = 100) -> list:
    return make_daily([close] * 220, last_day=date(2026, 11, 25))


def dip_daily() -> list:
    closes = [Decimal("100")] * 217 + [Decimal("99"), Decimal("98"), Decimal("97")]
    return make_daily(closes, last_day=date(2026, 11, 25))


def build_market(**overrides) -> InMemoryMarketData:
    now = datetime.now(tz=ET)
    quotes = {}
    daily = {}
    intraday = {}
    for index, symbol in enumerate(UNIVERSE):
        price = dec(97) if symbol == "NVDA" else dec(100 + index)
        quotes[symbol] = Quote(symbol=symbol, last_price=price, timestamp=now)
        daily[symbol] = dip_daily() if symbol == "NVDA" else flat_daily(price)
        if symbol == "NVDA":
            # 고가 200 스파이크(시가·종가 100)는 HOD 이상치 필터가 100 으로 대체해야 한다.
            bars = make_intraday([dec("100"), price, price], price, day=now.date())
            first = bars[0]
            bars[0] = Bar(
                timestamp=first.timestamp, open=dec("100"), high=dec("200"),
                low=dec("99.5"), close=dec("100"),
            )
            intraday[symbol] = bars
        else:
            intraday[symbol] = make_intraday(
                [price, price * dec("1.005"), price], price, day=now.date()
            )
    market = InMemoryMarketData(
        quotes=quotes,
        daily=daily,
        intraday=intraday,
        futures={"NQ=F": dec("-0.42")},
        closes={"^VIX": (dec("17.20"), now)},
    )
    for key, value in overrides.items():
        setattr(market, f"_{key}", value)
    return market


class StubEngine:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.bootstrap_calls = 0
        self.cycles = 0
        self.broker = None

    def bootstrap(self) -> None:
        self.bootstrap_calls += 1

    def run_cycle(self) -> CycleReport:
        self.cycles += 1
        now = datetime.now(tz=ET)
        result = OrderResult(
            client_order_id=f"as-test-{self.mode}-{self.cycles}",
            status="DRY_RUN",
            message="dry-run: 주문을 전송하지 않았습니다",
        )
        return CycleReport(
            at=now,
            skipped="after-close",
            equity_usd=dec("380"),
            cash_usd=dec("380"),
            buys=(result,),
            blocked=("session:after-close",),
        )


class Harness:
    """앱 + 서버 + 저장소 팩토리를 한 묶음으로 만든다."""

    def __init__(self, tmp_path, env=None, market=None) -> None:
        self.state_dir = tmp_path / "state"
        self.settings = load_settings(env=dict(env or {}), root=ROOT, mode="dry-run", data_dir=str(self.state_dir))
        _settings, self.strategy, self.guards = load_all(root=ROOT, mode="dry-run", data_dir=str(self.state_dir))
        self.market = market or build_market()
        self.engines: dict[str, StubEngine] = {}

        def engine_factory(mode: str) -> StubEngine:
            if mode not in self.engines:
                self.engines[mode] = StubEngine(mode)
            return self.engines[mode]

        self.app = WebApp(
            settings=self.settings,
            strategy=self.strategy,
            guards=self.guards,
            repo_factory=self.repo,
            market_data=self.market,
            engine_factory=engine_factory,
            calendar=StaticUsCalendar(ROOT / "config" / "market_calendar_2026.json"),
            token=TOKEN,
        )
        self.app.prime()
        self.server = create_server(self.app, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def repo(self) -> Repo:
        return Repo(Database(self.settings.db_path))

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.app.runner.stop(timeout=1)
        self.app.stop_feed()
        self.server.shutdown()
        self.server.server_close()

    def request(self, method: str, path: str, *, body=None, token: str | None = TOKEN, origin=None, content_type="application/json"):
        parsed = urlparse(self.base_url)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=15)
        headers = {}
        if token is not None:
            headers["X-Alpha-Token"] = token
        if body is not None:
            headers["Content-Type"] = content_type
        if origin is not None:
            headers["Origin"] = origin
        payload = None if body is None else json.dumps(body).encode("utf-8")
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        status = response.status
        conn.close()
        data = json.loads(raw) if raw else {}
        return status, data


@pytest.fixture
def harness(tmp_path):
    h = Harness(tmp_path)
    try:
        yield h
    finally:
        h.close()


# ---------------------------------------------------------------- 화면·상태
def test_page_is_served_with_token(harness):
    parsed = urlparse(harness.base_url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
    conn.request("GET", "/")
    response = conn.getresponse()
    html = response.read().decode("utf-8")
    conn.close()
    assert response.status == 200
    assert "ALPHA SHIELD" in html
    assert "8종목 유니버스 판정" in html
    assert "킬스위치" in html
    assert "Pretendard" in html
    assert TOKEN in html


def test_status_requires_token(harness):
    status, body = harness.request("GET", "/api/status", token=None)
    assert status == 401
    assert body["error"] == "unauthorized"


def test_status_payload_shape(harness):
    status, body = harness.request("GET", "/api/status")
    assert status == 200
    assert body["ok"] is True
    assert body["mode"] == "dry-run"
    assert body["account"]["equity_usd"] == "380.00"
    assert body["account"]["buffer_usd"] == "57.00"  # max(380*15%, 55)
    assert body["slots"] == {
        "total": 2,
        "used": 0,
        "free": 2,
        "tier": {"entry_usd": "75.00", "cap_usd": "150.00"},
    }
    assert len(body["universe"]) == 8
    assert {item["symbol"] for item in body["universe"]} == set(UNIVERSE)
    assert set(body["guards"]) == {
        "market", "session", "events", "filings", "news", "macro", "data", "account", "kill_switch"
    }
    assert body["kill_switch"]["active"] is False
    assert body["account_rules"]["settlement"] == "cash"
    assert body["safety"]["live_allowed"] is False
    assert body["safety"]["armed"] is False
    assert "KST" in body["clock"]["kst"]


def test_universe_indicators_and_ready_candidate(harness):
    _status, body = harness.request("GET", "/api/status")
    rows = {item["symbol"]: item for item in body["universe"]}
    nvda = rows["NVDA"]
    assert nvda["status"] == "ready"
    assert nvda["status_label"] == "진입 조건 충족"
    assert Decimal(nvda["rsi2"]) <= Decimal("15")
    assert Decimal(nvda["drawdown_pct"]) < Decimal("-2")
    assert Decimal(nvda["score"]) > 0
    assert nvda["change_pct"] is not None  # 일봉이 늦게 와도 등락률이 채워진다
    assert nvda["sma200"] is not None and nvda["threshold_pct"] is not None
    waiting = rows["AAPL"]
    assert waiting["status"] == "waiting"
    assert waiting["status_label"] != "진입 조건 충족"


def test_market_guards_reflect_snapshot(harness):
    _status, body = harness.request("GET", "/api/status")
    market = body["guards"]["market"]
    assert market["status"] == "pass"
    assert market["nq_change_pct"] == "-0.42"
    assert market["vix"] == "17.2"
    assert body["guards"]["data"]["status"] == "pass"
    assert body["guards"]["macro"]["status"] in ("pass", "boost", "unknown")


# ---------------------------------------------------------------- 제어
def test_control_start_and_stop_loop(harness):
    status, body = harness.request("POST", "/api/control", body={"action": "start", "mode": "paper"})
    assert status == 200
    assert body["ok"] is True
    assert harness.app.runner.state.mode == "paper"
    assert harness.engines["paper"].bootstrap_calls == 1

    _status, body = harness.request("GET", "/api/status")
    assert body["runner"]["state"] in ("running", "stopped")
    assert body["mode"] == "paper"

    status, body = harness.request("POST", "/api/control", body={"action": "stop"})
    assert status == 200
    assert body["runner"]["state"] == "stopped"


def test_live_start_is_blocked_by_double_lock(harness):
    status, body = harness.request("POST", "/api/control", body={"action": "start", "mode": "live"})
    assert status == 403
    assert body["error"] == "live-locked"
    assert "LIVE_ORDERS" in body["detail"]
    assert "live" not in harness.engines


def test_live_start_still_blocked_without_toss_credentials(tmp_path):
    env = {"LIVE_ORDERS": "true"}
    harness = Harness(tmp_path, env=env)
    try:
        harness.settings.armed_file.parent.mkdir(parents=True, exist_ok=True)
        harness.settings.armed_file.write_text("armed\n", encoding="utf-8")
        status, body = harness.request("POST", "/api/control", body={"action": "start", "mode": "live"})
        assert status == 403
        assert body["error"] == "live-locked"
        assert "TOSS_CLIENT_ID" in body["detail"]
        # 상태에는 잠금 해제 여부가 정확히 드러난다
        _status, state = harness.request("GET", "/api/status")
        assert state["safety"]["live_orders_env"] is True
        assert state["safety"]["armed"] is True
        assert state["safety"]["live_block_reason"].startswith("TOSS_CLIENT_ID")
    finally:
        harness.close()


def test_halt_toggle_writes_and_removes_flag(harness):
    flag = harness.settings.halt_file
    assert not flag.exists()
    status, body = harness.request("POST", "/api/control", body={"action": "halt-on"})
    assert status == 200
    assert flag.exists()
    _status, state = harness.request("GET", "/api/status")
    assert state["safety"]["halted"] is True
    assert "웹 대시보드 킬스위치" in state["safety"]["halt_reason"]

    status, body = harness.request("POST", "/api/control", body={"action": "halt-off"})
    assert status == 200
    assert not flag.exists()
    _status, state = harness.request("GET", "/api/status")
    assert state["safety"]["halted"] is False


def test_run_once_returns_cycle_report(harness):
    status, body = harness.request("POST", "/api/run-once", body={})
    assert status == 200
    assert body["ok"] is True
    report = body["report"]
    assert report["skipped"] == "after-close"
    assert report["equity_usd"] == "380.00"
    assert report["orders"] == 1
    assert harness.app.runner.state.cycles == 1

    _status, again = harness.request("POST", "/api/run-once", body={"mode": "dry-run"})
    assert again["ok"] is True
    assert harness.app.runner.state.cycles == 2


def test_run_once_live_is_locked(harness):
    status, body = harness.request("POST", "/api/run-once", body={"mode": "live"})
    assert status == 403
    assert body["error"] == "live-locked"


def test_unknown_action_and_mode_are_rejected(harness):
    status, body = harness.request("POST", "/api/control", body={"action": "explode"})
    assert status == 400
    assert body["error"] == "unknown-action"

    status, body = harness.request("POST", "/api/control", body={"action": "start", "mode": "turbo"})
    assert status == 400
    assert body["error"] == "invalid-mode"


def test_post_rejects_foreign_origin_and_missing_token(harness):
    status, body = harness.request(
        "POST", "/api/control", body={"action": "halt-on"}, origin="https://evil.example"
    )
    assert status == 403
    assert body["error"] == "bad-origin"
    assert not harness.settings.halt_file.exists()

    status, body = harness.request("POST", "/api/control", body={"action": "halt-on"}, token=None)
    assert status == 401

    status, body = harness.request(
        "POST", "/api/control", body={"action": "halt-on"}, content_type="text/plain"
    )
    assert status == 415


def test_unknown_path_is_404(harness):
    status, body = harness.request("GET", "/api/nope")
    assert status == 404
    assert body["error"] == "not-found"


# ---------------------------------------------------------------- 데이터 표시
def test_positions_and_orders_are_rendered(harness):
    now = datetime.now(tz=ET)
    repo = harness.repo()
    try:
        repo.save_position(
            Position(
                symbol="NVDA",
                qty=dec("0.5"),
                avg_price=dec("100"),
                entry_at=now,
                first_entry_date=date(2026, 11, 20),
                tp_target_pct=dec("8"),
                entry_vix=dec("16.5"),
            )
        )
        repo.record_order(
            OrderRequest(
                symbol="NVDA", side="BUY", client_order_id="as-test-1", amount_usd=dec("50"), reason="entry"
            ),
            OrderResult(
                client_order_id="as-test-1",
                status="FILLED",
                filled_qty=dec("0.5"),
                avg_price=dec("100"),
                filled_amount=dec("50"),
            ),
            mode="paper",
            created_at=now,
        )
    finally:
        repo.close()

    _status, body = harness.request("GET", "/api/status")
    assert body["slots"]["used"] == 1
    assert body["slots"]["free"] == 1
    position = body["positions"][0]
    assert position["symbol"] == "NVDA"
    assert Decimal(position["tp_target_pct"]) == dec("8")
    assert Decimal(position["tp_price"]) == dec("108")
    assert Decimal(position["stop_loss_pct"]) == dec("5")
    assert Decimal(position["stop_price"]) == dec("95")
    assert Decimal(position["pnl_pct"]) == dec("-3")  # 97 vs 100
    assert body["orders"][0]["symbol"] == "NVDA"
    assert body["orders"][0]["side"] == "BUY"


def test_market_data_failure_degrades_gracefully(tmp_path):
    class BrokenMarket(InMemoryMarketData):
        def quotes(self, symbols):
            raise RuntimeError("yahoo down")

        def daily_bars(self, symbol, count=250):
            raise RuntimeError("yahoo down")

        def intraday_bars(self, symbol, minutes=390):
            raise RuntimeError("yahoo down")

        def futures_change_pct(self, symbol):
            raise RuntimeError("yahoo down")

        def last_price(self, symbol):
            raise RuntimeError("yahoo down")

    harness = Harness(tmp_path, market=BrokenMarket())
    try:
        status, body = harness.request("GET", "/api/status")
        assert status == 200
        assert body["ok"] is True
        assert body["guards"]["market"]["status"] == "unknown"
        assert body["guards"]["data"]["status"] == "block"
        assert all(item["status"] == "nodata" for item in body["universe"])
        assert "시세 수집 중" in body["universe_note"] or "일부 시세 실패" in body["universe_note"]
    finally:
        harness.close()


def test_cli_exposes_web_options():
    args = build_parser().parse_args(["--web", "--host", "0.0.0.0", "--port", "9090", "--web-refresh", "30"])
    assert args.web is True
    assert args.host == "0.0.0.0"
    assert args.port == 9090
    assert args.web_refresh == 30
    defaults = build_parser().parse_args([])
    assert defaults.web is False
    assert defaults.host == "127.0.0.1"
    assert defaults.port == 8080
