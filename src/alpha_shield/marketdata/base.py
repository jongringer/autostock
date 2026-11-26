"""시장 데이터 인터페이스.

전략은 이 프로토콜만 의존한다. 실제 구현은 Yahoo(무료·비공식)와 토스증권 Open API.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Mapping, Protocol, Sequence

from ..models import Bar, Quote


class MarketDataError(RuntimeError):
    """시세를 확보하지 못했다 (fail-closed 매수 보류의 근거)."""


class MarketData(Protocol):
    def quotes(self, symbols: Sequence[str]) -> Mapping[str, Quote]:
        """현재가. 없는 종목은 결과에서 빠진다."""

    def daily_bars(self, symbol: str, count: int = 250) -> list[Bar]:
        """일봉, 오래된 -> 최신."""

    def intraday_bars(self, symbol: str, minutes: int = 390) -> list[Bar]:
        """당일 1분봉, 오래된 -> 최신."""

    def futures_change_pct(self, symbol: str) -> Decimal | None:
        """선물 전일 대비 변동률(%). 확인 불가면 None."""

    def last_daily_close(self, symbol: str) -> tuple[Decimal, object] | None:
        """(최근 일봉 종가, 기준 시각). 확인 불가면 None."""

    def last_price(self, symbol: str) -> Decimal | None:
        """가장 최근 가격(장중이면 장중 값). VIX 급등 판정에 쓴다."""


class CompositeMarketData:
    """종목 시세·캔들은 primary, 지수(NQ/VIX)는 indices 에서 가져온다."""

    def __init__(self, primary: MarketData, indices: MarketData) -> None:
        self.primary = primary
        self.indices = indices

    def quotes(self, symbols: Sequence[str]) -> Mapping[str, Quote]:
        return self.primary.quotes(symbols)

    def daily_bars(self, symbol: str, count: int = 250) -> list[Bar]:
        return self.primary.daily_bars(symbol, count)

    def intraday_bars(self, symbol: str, minutes: int = 390) -> list[Bar]:
        return self.primary.intraday_bars(symbol, minutes)

    def futures_change_pct(self, symbol: str) -> Decimal | None:
        try:
            return self.indices.futures_change_pct(symbol)
        except Exception:  # noqa: BLE001 - 가드에서 fail-closed 처리
            return None

    def last_daily_close(self, symbol: str):
        try:
            return self.indices.last_daily_close(symbol)
        except Exception:  # noqa: BLE001
            return None

    def last_price(self, symbol: str) -> Decimal | None:
        try:
            return self.indices.last_price(symbol)
        except Exception:  # noqa: BLE001
            return None


class InMemoryMarketData:
    """테스트·리플레이용."""

    def __init__(
        self,
        quotes: Mapping[str, Quote] | None = None,
        daily: Mapping[str, Sequence[Bar]] | None = None,
        intraday: Mapping[str, Sequence[Bar]] | None = None,
        futures: Mapping[str, Decimal | None] | None = None,
        closes: Mapping[str, tuple[Decimal, object]] | None = None,
    ) -> None:
        self._quotes = dict(quotes or {})
        self._daily = {symbol: list(bars) for symbol, bars in (daily or {}).items()}
        self._intraday = {symbol: list(bars) for symbol, bars in (intraday or {}).items()}
        self._futures = dict(futures or {})
        self._closes = dict(closes or {})

    def quotes(self, symbols: Sequence[str]) -> Mapping[str, Quote]:
        return {symbol: self._quotes[symbol] for symbol in symbols if symbol in self._quotes}

    def daily_bars(self, symbol: str, count: int = 250) -> list[Bar]:
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
