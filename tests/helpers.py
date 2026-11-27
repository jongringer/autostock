"""테스트 공용 헬퍼 — 실제 네트워크 없이 결정적인 시세·브로커를 만든다."""

from __future__ import annotations

import json
import pathlib
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from alpha_shield.clock import ET  # noqa: E402
from alpha_shield.models import Bar, Quote  # noqa: E402
from alpha_shield.net import HttpResponse  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def et(year: int, month: int, day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=ET)


def d(value: object) -> Decimal:
    return Decimal(str(value))


def make_daily(
    closes: Sequence[object],
    last_day: date = date(2026, 11, 30),
    spread: object = "0.01",
) -> list[Bar]:
    """closes 로 일봉 생성 (high/low = close ± spread 비율)."""
    spread_d = d(spread)
    bars: list[Bar] = []
    start = last_day - timedelta(days=len(closes) - 1)
    for index, close in enumerate(closes):
        price = d(close)
        bars.append(
            Bar(
                timestamp=datetime.combine(start + timedelta(days=index), time(0, 0), tzinfo=ET),
                open=price,
                high=price * (Decimal(1) + spread_d),
                low=price * (Decimal(1) - spread_d),
                close=price,
            )
        )
    return bars


def make_intraday(
    highs: Sequence[object],
    last_price: object,
    day: date = date(2026, 12, 1),
    start_hour: int = 9,
) -> list[Bar]:
    bars: list[Bar] = []
    for index, high in enumerate(highs):
        high_d = d(high)
        bars.append(
            Bar(
                timestamp=datetime.combine(day, time(start_hour, 31), tzinfo=ET) + timedelta(minutes=index),
                open=high_d,
                high=high_d,
                low=high_d - Decimal("0.5"),
                close=high_d,
            )
        )
    bars.append(
        Bar(
            timestamp=datetime.combine(day, time(11, 0), tzinfo=ET),
            open=d(last_price),
            high=d(last_price),
            low=d(last_price),
            close=d(last_price),
        )
    )
    return bars


def falling_closes(count: int = 220, start: object = "100.5", end: object = "96") -> list[Decimal]:
    """완만한 보합 후 마지막 4일 하락 — RSI(2) <= 15, SMA200 x 0.95 위.

    v3.3 은 SMA200 바닥을 0.95 로 올렸으므로, 200일선에서 5% 안쪽인 종목으로 만든다.
    """
    values = [d(start)] * count
    step = (d(start) - d(end)) / Decimal(4)
    for offset in range(4):
        values[-4 + offset] = d(end) + step * Decimal(3 - offset)
    return values


@dataclass(frozen=True)
class Call:
    method: str
    url: str
    headers: Mapping[str, str]
    params: Mapping[str, object] | None
    json_body: Any
    form: Mapping[str, str] | None


def json_response(payload: object, status: int = 200, headers: Mapping[str, str] | None = None) -> HttpResponse:
    return HttpResponse(
        status=status,
        headers={"Content-Type": "application/json", **(headers or {})},
        body=json.dumps(payload).encode("utf-8"),
    )


def text_response(body: str, status: int = 200, content_type: str = "text/html") -> HttpResponse:
    return HttpResponse(status=status, headers={"Content-Type": content_type}, body=body.encode("utf-8"))


class FakeHTTP:
    """경로/메서드 조건에 따라 응답을 돌려주는 전송 계층."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self._routes: list[tuple[Callable[[str, str, Mapping[str, object] | None], bool], Callable[["FakeHTTP", str, str], HttpResponse]]] = []

    def route(self, predicate: Callable[[str, str, Mapping[str, object] | None], bool], responder) -> "FakeHTTP":
        self._routes.append((predicate, responder))
        return self

    def on(self, fragment: str, responder, method: str | None = None) -> "FakeHTTP":
        def predicate(call_method: str, url: str, params: Mapping[str, object] | None) -> bool:
            return fragment in url and (method is None or call_method == method)

        if callable(responder):
            self.route(predicate, responder)
        else:
            self.route(predicate, lambda m, u: responder)
        return self

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, object] | None = None,
        json_body: Any | None = None,
        form: Mapping[str, str] | None = None,
        timeout: float = 15.0,
    ) -> HttpResponse:
        self.calls.append(Call(method, url, dict(headers or {}), params, json_body, form))
        for predicate, responder in self._routes:
            if predicate(method, url, params):
                return responder(method, url)
        return json_response({"error": {"code": "not-found", "message": url}}, status=404)

    # ------------------------------------------------------------------ 조회
    def find(self, fragment: str, method: str | None = None) -> Call | None:
        for call in reversed(self.calls):
            if fragment in call.url and (method is None or call.method == method):
                return call
        return None

    def all(self, fragment: str, method: str | None = None) -> list[Call]:
        return [c for c in self.calls if fragment in c.url and (method is None or c.method == method)]


class MutableMarketData:
    """테스트에서 사이클마다 시세를 바꿀 수 있는 시장 데이터."""

    def __init__(self) -> None:
        self._quotes: dict[str, Quote] = {}
        self._daily: dict[str, list[Bar]] = {}
        self._intraday: dict[str, list[Bar]] = {}
        self._futures: dict[str, Decimal | None] = {"NQ=F": d("0.4")}
        self._closes: dict[str, tuple[Decimal, object]] = {"^VIX": (d("15.5"), et(2026, 11, 30, 16, 0))}
        self.fail_quotes = False
        self.fail_daily = False

    def set_quote(self, symbol: str, price: object, at: datetime | None = None) -> None:
        self._quotes[symbol] = Quote(symbol, d(price), at or et(2026, 12, 1, 10, 29))

    def set_daily(self, symbol: str, bars: Sequence[Bar]) -> None:
        self._daily[symbol] = list(bars)

    def set_intraday(self, symbol: str, bars: Sequence[Bar]) -> None:
        self._intraday[symbol] = list(bars)

    def set_futures(self, change: Decimal | None) -> None:
        self._futures["NQ=F"] = change

    def set_vix(self, value: object | None) -> None:
        if value is None:
            self._closes.pop("^VIX", None)
        else:
            self._closes["^VIX"] = (d(value), et(2026, 11, 30, 16, 0))

    # --- 인터페이스 ---
    def quotes(self, symbols: Sequence[str]) -> Mapping[str, Quote]:
        if self.fail_quotes:
            raise RuntimeError("quotes down")
        return {symbol: self._quotes[symbol] for symbol in symbols if symbol in self._quotes}

    def daily_bars(self, symbol: str, count: int = 250) -> list[Bar]:
        if self.fail_daily:
            raise RuntimeError("daily down")
        return self._daily.get(symbol, [])[-count:]

    def intraday_bars(self, symbol: str, minutes: int = 390) -> list[Bar]:
        return self._intraday.get(symbol, [])[-minutes:]

    def futures_change_pct(self, symbol: str) -> Decimal | None:
        return self._futures.get(symbol)

    def last_daily_close(self, symbol: str):
        return self._closes.get(symbol)

    def last_price(self, symbol: str) -> Decimal | None:
        payload = self._closes.get(symbol)
        return None if payload is None else payload[0]


class EmptyEarnings:
    def symbols_with_earnings(self, day: date) -> set[str]:
        return set()


class FixedEarnings:
    def __init__(self, mapping: Mapping[date, set[str]] | None = None, fail: bool = False) -> None:
        self.mapping = dict(mapping or {})
        self.fail = fail

    def symbols_with_earnings(self, day: date):
        if self.fail:
            return None
        return self.mapping.get(day, set())


class NoFilings:
    def check(self, symbol, cik, keywords, today, lookback_days=3):
        from alpha_shield.marketdata.sec import FilingCheck

        return FilingCheck(checked=True, detail={"symbol": symbol, "filings": 0})


class EmptyNews:
    def headlines(self, symbol: str) -> list[str]:
        return []


class FixedNews:
    def __init__(self, titles: Sequence[str] | None = None, fail: bool = False) -> None:
        self.titles = list(titles or [])
        self.fail = fail

    def headlines(self, symbol: str):
        if self.fail:
            return None
        return list(self.titles)


class BlockingFilings:
    def __init__(self, blocked: bool = True, checked: bool = True) -> None:
        self.blocked = blocked
        self.checked = checked

    def check(self, symbol, cik, keywords, today, lookback_days=3):
        from alpha_shield.marketdata.sec import FilingCheck

        if not self.checked:
            return FilingCheck(checked=False, reason="sec-unavailable", detail={"symbol": symbol})
        if self.blocked:
            return FilingCheck(checked=True, blocked=True, reason="sec-8k-adverse", detail={"symbol": symbol})
        return FilingCheck(checked=True, detail={"symbol": symbol})
