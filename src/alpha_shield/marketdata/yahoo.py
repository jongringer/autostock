"""Yahoo Finance 차트 API — NQ 선물·VIX·일봉·1분봉.

비공식 엔드포인트이므로:
- User-Agent 필수, 호출 간 최소 간격(1초), 재시도 3회, 응답 캐시
- 3회 연속 실패는 예외로 올려 가드가 매수를 멈추게 한다(fail-closed)
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from ..cache import TTLCache
from ..clock import ET
from ..models import Bar, Quote
from ..net import HttpResponse, HttpTransport, TransportError, UrllibTransport
from ..numeric import dec, pct4

LOG = logging.getLogger(__name__)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


class YahooError(RuntimeError):
    pass


class YahooMarketData:
    def __init__(
        self,
        transport: HttpTransport | None = None,
        *,
        min_interval: float = 1.0,
        cache: TTLCache | None = None,
        sleep=time.sleep,
        clock=None,
    ) -> None:
        self.transport = transport or UrllibTransport()
        self.min_interval = min_interval
        self.cache = cache if cache is not None else TTLCache()
        self._sleep = sleep
        self._clock = clock or (lambda: datetime.now(tz=ET))
        self._last_call = 0.0

    # ------------------------------------------------------------------ 내부
    def _throttle(self) -> None:
        now = time.monotonic()
        wait = self.min_interval - (now - self._last_call)
        if wait > 0:
            self._sleep(wait)
        self._last_call = time.monotonic()

    def _chart(self, symbol: str, range_: str, interval: str, ttl: float) -> Mapping[str, Any]:
        key = f"chart:{symbol}:{range_}:{interval}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        self._throttle()
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response: HttpResponse = self.transport.request(
                    "GET",
                    CHART_URL.format(symbol=symbol),
                    params={"range": range_, "interval": interval, "includePrePost": "false"},
                    headers={"Accept": "application/json"},
                )
            except TransportError as exc:
                last_error = exc
                self._sleep(min(8.0, 2 ** attempt))
                continue
            if response.ok:
                payload = response.json() or {}
                result = ((payload.get("chart") or {}).get("result") or [None])[0]
                if not result:
                    last_error = YahooError(f"{symbol}: empty chart result")
                    break
                self.cache.set(key, result, ttl)
                return result
            if response.status in (429, 500, 502, 503, 504):
                self._sleep(min(8.0, 2 ** attempt))
                last_error = YahooError(f"{symbol}: HTTP {response.status}")
                continue
            raise YahooError(f"{symbol}: HTTP {response.status}")
        raise YahooError(f"{symbol} 차트 조회 실패: {last_error}")

    @staticmethod
    def _timestamp(epoch: int) -> datetime:
        # 차트 timestamps 는 UTC epoch — ET 로 표기한다.
        return datetime.fromtimestamp(epoch, tz=ET)

    @staticmethod
    def _price(value: object) -> Decimal:
        """Yahoo 는 float 을 준다 — 표시·계산 잡음을 줄이려 소수 4자리로 정리한다."""
        return dec(value).quantize(Decimal("0.0001"))

    def _bars(self, symbol: str, range_: str, interval: str, ttl: float) -> list[Bar]:
        result = self._chart(symbol, range_, interval, ttl)
        timestamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []
        bars: list[Bar] = []
        for index, epoch in enumerate(timestamps):
            try:
                close = closes[index]
                if close is None:
                    continue
                bar = Bar(
                    timestamp=self._timestamp(epoch),
                    open=self._price(opens[index] if opens[index] is not None else close),
                    high=self._price(highs[index] if highs[index] is not None else close),
                    low=self._price(lows[index] if lows[index] is not None else close),
                    close=self._price(close),
                    volume=dec(volumes[index] if index < len(volumes) and volumes[index] is not None else 0),
                )
            except (IndexError, TypeError, ValueError):
                continue
            bars.append(bar)
        bars.sort(key=lambda item: item.timestamp)
        return bars

    # ------------------------------------------------------------------ 인터페이스
    def quotes(self, symbols: Sequence[str]) -> Mapping[str, Quote]:
        out: dict[str, Quote] = {}
        for symbol in symbols:
            try:
                bars = self._bars(symbol, "1d", "1m", ttl=60)
            except YahooError as exc:
                LOG.warning("Yahoo 시세 실패 %s: %s", symbol, exc)
                continue
            if not bars:
                continue
            last = bars[-1]
            out[symbol] = Quote(symbol=symbol, last_price=last.close, timestamp=last.timestamp)
        return out

    def daily_bars(self, symbol: str, count: int = 250) -> list[Bar]:
        bars = self._bars(symbol, "2y", "1d", ttl=900)
        return bars[-count:]

    def intraday_bars(self, symbol: str, minutes: int = 390) -> list[Bar]:
        bars = self._bars(symbol, "1d", "1m", ttl=60)
        return bars[-minutes:]

    def futures_change_pct(self, symbol: str) -> Decimal | None:
        result = self._chart(symbol, "1d", "5m", ttl=60)
        meta = result.get("meta") or {}
        previous = meta.get("chartPreviousClose") or meta.get("previousClose")
        bars = self._bars(symbol, "1d", "5m", ttl=60)
        if not bars or previous in (None, 0):
            return None
        last = bars[-1].close
        change = (last - dec(previous)) / dec(previous) * Decimal(100)
        return pct4(change)

    def last_daily_close(self, symbol: str):
        bars = self._bars(symbol, "1mo", "1d", ttl=300)
        if not bars:
            return None
        last = bars[-1]
        return last.close, last.timestamp

    def last_price(self, symbol: str) -> Decimal | None:
        """장중이면 장중 최근값, 아니면 최근 일봉 종가. VIX 급등 판정용."""
        try:
            bars = self._bars(symbol, "1d", "5m", ttl=60)
        except YahooError:
            bars = []
        if bars:
            return bars[-1].close
        daily = self.last_daily_close(symbol)
        return None if daily is None else daily[0]
