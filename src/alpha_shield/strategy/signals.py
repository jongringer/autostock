"""BUY 신호 큐 — 15분(900초) 만료, 재전달 전 RSI(2) > 25 폐기 (fail-closed)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Mapping

from ..state.repo import Repo, SignalRecord

STATUS_PENDING = "PENDING"
STATUS_EXPIRED = "EXPIRED"
STATUS_DISCARDED = "DISCARDED"
STATUS_CONSUMED = "CONSUMED"


class SignalBook:
    def __init__(self, repo: Repo, redelivery_rsi_max: Decimal = Decimal("25")) -> None:
        self.repo = repo
        self.redelivery_rsi_max = redelivery_rsi_max

    def publish(
        self,
        symbol: str,
        now: datetime,
        ttl_seconds: int,
        score: Decimal | None,
        threshold_pct: Decimal | None,
    ) -> SignalRecord:
        """이번 사이클의 후보를 신호로 등록한다.

        유효한 PENDING 신호가 이미 있으면 만료 시각을 늘리지 않는다(무한 연장 방지) —
        점수·임계만 갱신한다.
        """
        existing = self._pending_for(symbol)
        if existing is not None and existing.expires_at > now:
            return existing
        signal_id = self.repo.add_signal(
            symbol=symbol,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
            score=score,
            threshold_pct=threshold_pct,
        )
        return SignalRecord(signal_id, symbol, now, now + timedelta(seconds=ttl_seconds), score, threshold_pct, STATUS_PENDING)

    def _pending_for(self, symbol: str) -> SignalRecord | None:
        for record in self.repo.signals(STATUS_PENDING):
            if record.symbol == symbol:
                return record
        return None

    def housekeeping(self, now: datetime, rsi_by_symbol: Mapping[str, Decimal | None]) -> list[tuple[str, str]]:
        """만료·폐기 처리. (symbol, status) 목록을 돌려준다."""
        changes: list[tuple[str, str]] = []
        for record in self.repo.signals(STATUS_PENDING):
            if record.expires_at <= now:
                self.repo.set_signal_status(record.id, STATUS_EXPIRED)
                changes.append((record.symbol, STATUS_EXPIRED))
                continue
            rsi = rsi_by_symbol.get(record.symbol)
            if rsi is not None and rsi > self.redelivery_rsi_max:
                # 눌림이 회복됐다 → 이 신호로는 매수하지 않는다.
                self.repo.set_signal_status(record.id, STATUS_DISCARDED)
                changes.append((record.symbol, STATUS_DISCARDED))
        return changes

    def valid(self, now: datetime) -> list[SignalRecord]:
        return [record for record in self.repo.signals(STATUS_PENDING) if record.expires_at > now]

    def consume(self, signal_id: int) -> None:
        self.repo.set_signal_status(signal_id, STATUS_CONSUMED)
