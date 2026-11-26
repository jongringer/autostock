"""재진입 쿨다운 — 청산 후 15분(900초). 손절 24시간 차단은 폐지됐다."""

from __future__ import annotations

from datetime import datetime, timedelta

from ..state.repo import Repo


class CooldownBook:
    def __init__(self, repo: Repo, seconds: int = 900) -> None:
        self.repo = repo
        self.seconds = seconds

    def block(self, symbol: str, now: datetime, reason: str = "exit") -> datetime:
        until = now + timedelta(seconds=self.seconds)
        self.repo.set_cooldown(symbol, until, reason)
        return until

    def is_blocked(self, symbol: str, now: datetime) -> bool:
        until = self.repo.cooldown_until(symbol)
        return until is not None and until > now

    def remaining_seconds(self, symbol: str, now: datetime) -> int:
        until = self.repo.cooldown_until(symbol)
        if until is None or until <= now:
            return 0
        return int((until - now).total_seconds())

    def clear(self, symbol: str) -> None:
        self.repo.clear_cooldown(symbol)
