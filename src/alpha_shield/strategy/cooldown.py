"""재진입 쿨다운 — 일반 청산 15분(900초) + 손절 시 당일(EOD) 재진입 금지.

v3.3 은 v3.2 에서 폐지했던 '동일 종목 손절 후 24시간 차단'을 EOD(당일) 단위로 복원한다.
일반 중복 주문 방지 15분 쿨다운은 그대로 유지한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..state.repo import Repo

REASON_EXIT = "exit"
REASON_STOP_LOSS_EOD = "stop_loss_eod"


class CooldownBook:
    def __init__(self, repo: Repo, seconds: int = 900) -> None:
        self.repo = repo
        self.seconds = seconds

    def block(self, symbol: str, now: datetime, reason: str = REASON_EXIT) -> datetime:
        return self.block_until(symbol, now + timedelta(seconds=self.seconds), reason)

    def block_until(self, symbol: str, until: datetime, reason: str = REASON_EXIT) -> datetime:
        self.repo.set_cooldown(symbol, until, reason)
        return until

    def is_blocked(self, symbol: str, now: datetime) -> bool:
        until = self.repo.cooldown_until(symbol)
        return until is not None and until > now

    def reason(self, symbol: str) -> str | None:
        return self.repo.cooldown_reason(symbol)

    def remaining_seconds(self, symbol: str, now: datetime) -> int:
        until = self.repo.cooldown_until(symbol)
        if until is None or until <= now:
            return 0
        return int((until - now).total_seconds())

    def clear(self, symbol: str) -> None:
        self.repo.clear_cooldown(symbol)
