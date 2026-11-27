"""미국 정규장 세션 — 개장/마감·조기폐장·소수점 주문 마감 판정.

live 모드의 단일 출처는 토스 `GET /api/v1/market-calendar/US` 이고,
API 를 쓸 수 없을 때는 `config/market_calendar_2026.json` 폴백을 쓴다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol

from .clock import ET, at_et, to_et
from .config import GuardsConfig

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionWindow:
    trading_date: date
    regular_start: datetime
    regular_end: datetime
    early_close: bool = False
    source: str = "static"

    # --- 기본 상태 ---------------------------------------------------------
    def is_open(self, now: datetime) -> bool:
        return self.regular_start <= to_et(now) < self.regular_end

    def is_closed(self, now: datetime) -> bool:
        return to_et(now) >= self.regular_end

    def is_before_open(self, now: datetime) -> bool:
        return to_et(now) < self.regular_start

    # --- 스펙 창 -----------------------------------------------------------
    def entry_window(
        self,
        open_delay_min: int = 15,
        close_buffer_min: int = 15,
        fractional_close_buffer_min: int = 60,
    ) -> tuple[datetime, datetime]:
        """신규 매수 가능 창: 개장 +15분 ~ 소수점 마감(15:00 ET) -15분 = 14:45 ET.

        스펙의 '마감 전 15분 유예(정규 14:45~15:00 / 조기 12:45~13:00)'는
        소수점 주문 마감 시각을 기준으로 한다 (그 뒤로는 금액·소수점 주문 자체가 불가).
        """
        return (
            self.regular_start + timedelta(minutes=open_delay_min),
            self.fractional_deadline(fractional_close_buffer_min) - timedelta(minutes=close_buffer_min),
        )

    def entry_allowed(
        self,
        now: datetime,
        open_delay_min: int = 15,
        close_buffer_min: int = 15,
        fractional_close_buffer_min: int = 60,
    ) -> bool:
        start, end = self.entry_window(open_delay_min, close_buffer_min, fractional_close_buffer_min)
        return start <= to_et(now) < end

    def fractional_deadline(self, close_buffer_min: int = 60) -> datetime:
        """소수점(금액 매수·소수 매도) 주문 마감.

        정규장은 종료 1시간 전(15:00 ET). 조기폐장일(13:00 ET)은 스펙대로 종료 시각까지
        소수점 주문·선제 청산 창(12:50~12:58 ET)을 쓴다.
        """
        if self.early_close:
            return self.regular_end
        return self.regular_end - timedelta(minutes=close_buffer_min)

    def fractional_sell_allowed(self, now: datetime, close_buffer_min: int = 60) -> bool:
        return self.regular_start <= to_et(now) < self.fractional_deadline(close_buffer_min)

    def forced_exit_window(
        self, now: datetime, close_buffer_min: int = 15, fractional_close_buffer_min: int = 60
    ) -> bool:
        """마감 전 15분(14:45~15:00 ET) — 신규 매수 금지, 청산만 허용."""
        start = self.fractional_deadline(fractional_close_buffer_min) - timedelta(minutes=close_buffer_min)
        return start <= to_et(now) < self.regular_end

    # --- v3.3 15:00 소수점 마감 프로토콜 ------------------------------------
    def preemptive_window(
        self,
        close_buffer_min: int = 60,
        start_min: int = 10,
        end_min: int = 2,
    ) -> tuple[datetime, datetime]:
        """선제 청산 창 — 14:50~14:58 ET (조기폐장 12:50~12:58 ET)."""
        deadline = self.fractional_deadline(close_buffer_min)
        return deadline - timedelta(minutes=start_min), deadline - timedelta(minutes=end_min)

    def in_preemptive_window(
        self,
        now: datetime,
        close_buffer_min: int = 60,
        start_min: int = 10,
        end_min: int = 2,
    ) -> bool:
        start, end = self.preemptive_window(close_buffer_min, start_min, end_min)
        return start <= to_et(now) < end

    def overnight_stop_allowed(self, now: datetime, open_min: int = 2) -> bool:
        """오버나잇 갭하락 손절 집행 시각(09:32 ET) 이후인가."""
        return to_et(now) >= self.regular_start + timedelta(minutes=open_min)

    def premarket_flush_allowed(self, now: datetime, open_gap_min: int = 2) -> bool:
        """프리마켓 예약 매도 집행 창 — 09:30~09:32 ET."""
        return self.regular_start <= to_et(now) < self.regular_start + timedelta(minutes=open_gap_min)

    def is_premarket(self, now: datetime) -> bool:
        """정규장 개장 전(당일 프리마켓)인가."""
        return to_et(now) < self.regular_start

    def minutes_to_close(self, now: datetime) -> int:
        return int((self.regular_end - to_et(now)).total_seconds() // 60)

    def to_dict(self) -> dict[str, object]:
        return {
            "trading_date": self.trading_date.isoformat(),
            "regular_start": self.regular_start.isoformat(),
            "regular_end": self.regular_end.isoformat(),
            "early_close": self.early_close,
            "source": self.source,
        }


class MarketCalendar(Protocol):
    def session_on(self, day: date) -> SessionWindow | None: ...

    def is_trading_day(self, day: date) -> bool: ...

    def trading_days_between(self, start: date, end: date) -> int: ...


class _BaseCalendar:
    """휴장일 집합 기반 공통 구현."""

    def __init__(self) -> None:
        self._session_cache: dict[date, SessionWindow | None] = {}

    # 구현체가 채운다
    holidays: frozenset[date] = frozenset()
    early_closes: dict[date, str] = {}

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.holidays

    def session_on(self, day: date) -> SessionWindow | None:
        raise NotImplementedError

    def trading_days_between(self, start: date, end: date) -> int:
        """start(포함) ~ end(제외) 사이의 거래일 수. end 가 start 이전이면 음수."""
        if end < start:
            return -self.trading_days_between(end, start)
        count = 0
        cursor = start
        while cursor < end:
            if self.is_trading_day(cursor):
                count += 1
            cursor += timedelta(days=1)
        return count


class StaticUsCalendar(_BaseCalendar):
    """`config/market_calendar_2026.json` 기반 폴백 캘린더."""

    def __init__(self, config_path: Path | None = None) -> None:
        super().__init__()
        path = config_path or Path(__file__).resolve().parents[2] / "config" / "market_calendar_2026.json"
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        self.open_time = str(raw.get("regular", {}).get("open", "09:30"))
        self.close_time = str(raw.get("regular", {}).get("close", "16:00"))
        self.holidays = frozenset(date.fromisoformat(item) for item in raw.get("holidays", []))
        self.early_closes = {
            date.fromisoformat(day): str(close) for day, close in raw.get("early_closes", {}).items()
        }
        self.source = f"static:{Path(path).name}"

    def session_on(self, day: date) -> SessionWindow | None:
        if day in self._session_cache:
            return self._session_cache[day]
        if not self.is_trading_day(day):
            self._session_cache[day] = None
            return None
        close = self.early_closes.get(day, self.close_time)
        window = SessionWindow(
            trading_date=day,
            regular_start=at_et(day, self.open_time),
            regular_end=at_et(day, close),
            early_close=day in self.early_closes,
            source=self.source,
        )
        self._session_cache[day] = window
        return window


class TossUsCalendar(_BaseCalendar):
    """토스 market-calendar/US 기반 (live 권장). 실패 시 정적 캘린더 폴백."""

    def __init__(self, client, fallback: StaticUsCalendar | None = None) -> None:
        super().__init__()
        self.client = client
        self.fallback = fallback or StaticUsCalendar()
        self.holidays = self.fallback.holidays
        self.early_closes = self.fallback.early_closes
        self.source = "toss:market-calendar/US"

    def _fetch(self, day: date) -> SessionWindow | None:
        try:
            payload = self.client.get("/api/v1/market-calendar/US", params={"date": day.isoformat()})
        except Exception as exc:  # noqa: BLE001 - 캘린더 실패는 폴백
            LOG.warning("토스 캘린더 조회 실패 (%s): %s — 정적 캘린더로 폴백", day, exc)
            return self.fallback.session_on(day)
        today = (payload or {}).get("today") or {}
        regular = today.get("regularMarket")
        if not regular:
            return None
        start = datetime.fromisoformat(regular["startTime"]).astimezone(ET)
        end = datetime.fromisoformat(regular["endTime"]).astimezone(ET)
        return SessionWindow(
            trading_date=day,
            regular_start=start,
            regular_end=end,
            early_close=(end.hour, end.minute) != (16, 0),
            source=self.source,
        )

    def session_on(self, day: date) -> SessionWindow | None:
        if day in self._session_cache:
            return self._session_cache[day]
        # 거래일 판정은 토스 응답이 단일 출처다 (휴장이면 regularMarket 이 null).
        window = self._fetch(day)
        self._session_cache[day] = window
        return window


def build_calendar(mode: str, toss_client=None, root: Path | None = None) -> MarketCalendar:
    static = StaticUsCalendar(
        None if root is None else root / "config" / "market_calendar_2026.json"
    )
    if mode == "live" and toss_client is not None:
        return TossUsCalendar(toss_client, fallback=static)
    return static


class SessionGuard:
    """세션 가드 — 개장 15분·마감 15분 유예, 소수점 마감 1시간 전."""

    def __init__(self, calendar: MarketCalendar, config: GuardsConfig) -> None:
        self.calendar = calendar
        self.config = config.session

    def window_for(self, now: datetime) -> SessionWindow | None:
        return self.calendar.session_on(to_et(now).date())

    def evaluate(self, now: datetime) -> "SessionEvaluation":
        window = self.window_for(now)
        now = to_et(now)
        if window is None:
            return SessionEvaluation(window=None, is_open=False, reason="market-holiday")
        if now < window.regular_start:
            return SessionEvaluation(window=window, is_open=False, reason="before-open")
        if now >= window.regular_end:
            return SessionEvaluation(window=window, is_open=False, reason="after-close")
        open_delay = self.config.entry_open_delay_min
        close_buffer = self.config.entry_close_buffer_min
        entry_start, entry_end = window.entry_window(
            open_delay, close_buffer, self.config.fractional_close_buffer_min
        )
        if now < entry_start:
            return SessionEvaluation(
                window=window,
                is_open=True,
                reason="opening-grace",
                fractional_sell_allowed=window.fractional_sell_allowed(
                    now, self.config.fractional_close_buffer_min
                ),
            )
        if now >= entry_end:
            return SessionEvaluation(
                window=window,
                is_open=True,
                reason="closing-grace",
                entry_allowed=False,
                fractional_sell_allowed=window.fractional_sell_allowed(
                    now, self.config.fractional_close_buffer_min
                ),
            )
        return SessionEvaluation(
            window=window,
            is_open=True,
            entry_allowed=True,
            fractional_sell_allowed=window.fractional_sell_allowed(
                now, self.config.fractional_close_buffer_min
            ),
        )


@dataclass(frozen=True)
class SessionEvaluation:
    window: SessionWindow | None
    is_open: bool
    entry_allowed: bool = False
    fractional_sell_allowed: bool = False
    reason: str = ""

    @property
    def can_trade_sell(self) -> bool:
        """매도(청산)는 세션 중이면 항상 시도한다 (fail-open)."""
        return self.is_open

    def to_dict(self) -> dict[str, object]:
        return {
            "is_open": self.is_open,
            "entry_allowed": self.entry_allowed,
            "fractional_sell_allowed": self.fractional_sell_allowed,
            "reason": self.reason,
            "window": None if self.window is None else self.window.to_dict(),
        }
