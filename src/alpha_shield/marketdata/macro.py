"""거시 이벤트 캘린더 — FOMC/CPI/NFP 당일 요구 낙폭 -0.5%p 강화."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from ..numeric import ZERO, dec


class MacroCalendarError(RuntimeError):
    pass


class MacroCalendar:
    def __init__(self, path: Path, boost_pct: Decimal) -> None:
        self.path = Path(path)
        self.boost_pct = boost_pct
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MacroCalendarError(f"거시 캘린더를 읽을 수 없습니다: {self.path} ({exc})") from exc
        events = raw.get("events") or {}
        if not isinstance(events, dict):
            raise MacroCalendarError("macro calendar events 형식 오류")
        self._dates: dict[date, set[str]] = {}
        for name, items in events.items():
            for item in items or []:
                try:
                    day = date.fromisoformat(str(item))
                except ValueError:
                    continue
                self._dates.setdefault(day, set()).add(str(name))

    def events_on(self, day: date) -> tuple[str, ...]:
        return tuple(sorted(self._dates.get(day, set())))

    def drawdown_boost_pct(self, day: date) -> Decimal:
        return self.boost_pct if day in self._dates else ZERO

    def dates(self) -> dict[date, set[str]]:
        return {day: set(names) for day, names in self._dates.items()}


def load_macro_calendar(path: Path, boost_pct: object) -> MacroCalendar:
    return MacroCalendar(path, dec(boost_pct))
