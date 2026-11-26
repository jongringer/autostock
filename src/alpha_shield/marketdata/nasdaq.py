"""Nasdaq 실적 캘린더 — 실적 D-1 ~ 당일 신규 매수 차단.

`api.nasdaq.com/api/calendar/earnings?date=YYYY-MM-DD` (2026-11-27 실호출 확인).
조회 실패는 None 으로 돌려주고 가드가 fail-closed 로 처리한다.
"""

from __future__ import annotations

import logging
from datetime import date

from ..cache import TTLCache
from ..net import HttpTransport, TransportError

LOG = logging.getLogger(__name__)

EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}


class NasdaqEarningsCalendar:
    def __init__(
        self,
        transport: HttpTransport,
        *,
        cache: TTLCache | None = None,
        cache_seconds: int = 12 * 3600,
    ) -> None:
        self.transport = transport
        self.cache = cache if cache is not None else TTLCache()
        self.cache_seconds = cache_seconds

    def symbols_with_earnings(self, day: date) -> set[str] | None:
        key = f"earnings:{day.isoformat()}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached  # type: ignore[return-value]
        try:
            response = self.transport.request(
                "GET", EARNINGS_URL, params={"date": day.isoformat()}, headers=HEADERS
            )
        except TransportError as exc:
            LOG.warning("Nasdaq 실적 캘린더 요청 실패 %s: %s", day, exc)
            return None
        if not response.ok:
            LOG.warning("Nasdaq 실적 캘린더 응답 %s (%s)", response.status, day)
            return None
        try:
            payload = response.json() or {}
        except ValueError:
            return None
        rows = ((payload.get("data") or {}).get("rows")) or []
        symbols = {
            str(row.get("symbol", "")).upper()
            for row in rows
            if isinstance(row, dict) and row.get("symbol")
        }
        self.cache.set(key, symbols, self.cache_seconds)
        return symbols
