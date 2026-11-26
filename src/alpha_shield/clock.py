"""시간 처리 — 내부 판단은 모두 America/New_York, 로그는 KST 병기."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")


def now_et() -> datetime:
    return datetime.now(tz=ET)


def to_et(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("naive datetime 은 ET 로 해석할 수 없습니다 (tzinfo 필요)")
    return value.astimezone(ET)


def to_kst(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("naive datetime 은 KST 로 해석할 수 없습니다 (tzinfo 필요)")
    return value.astimezone(KST)


def parse_iso(value: str) -> datetime:
    """ISO8601 문자열. 오프셋이 없으면 ET 로 간주한다."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ET)
    return parsed


def et_date(value: datetime) -> date:
    return to_et(value).date()


def at_et(day: date, hhmm: str) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=ET)


def stamp_kst(value: datetime) -> str:
    """로그용 'YYYY-MM-DD HH:MM:SS KST' 문자열."""
    return to_kst(value).strftime("%Y-%m-%d %H:%M:%S KST")


def compact_kst(value: datetime) -> str:
    return to_kst(value).strftime("%Y-%m-%dT%H:%M:%S%z")


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """실제 시각 (ET aware)."""

    def now(self) -> datetime:
        return now_et()


@dataclass
class FixedClock:
    """테스트·리플레이용 고정 시계."""

    current: datetime

    def __post_init__(self) -> None:
        if self.current.tzinfo is None:
            raise ValueError("FixedClock 은 tzinfo 있는 시각이 필요합니다")

    def now(self) -> datetime:
        return self.current.astimezone(ET)

    def set(self, value: datetime) -> None:
        self.current = value

    def advance(self, seconds: float) -> datetime:
        self.current = self.current + timedelta(seconds=seconds)
        return self.current

    def to(self, value: datetime) -> datetime:
        self.current = value
        return self.current


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)
