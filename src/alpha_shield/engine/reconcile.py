"""브로커 보유 ↔ 로컬 원장 대조(reconcile).

불일치면 신규 매수를 멈추고 매도만 허용한다 (스펙 5.6 안전장치 1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Mapping, Sequence

from ..models import Position, PositionView
from ..numeric import ZERO

DEFAULT_TOLERANCE = Decimal("0.000001")


@dataclass(frozen=True)
class ReconcileResult:
    ok: bool
    mismatches: tuple[str, ...] = ()
    details: dict = field(default_factory=dict)

    def describe(self) -> str:
        if self.ok:
            return "보유 수량 일치"
        return "; ".join(self.mismatches)


def reconcile(
    local_positions: Sequence[Position],
    broker_positions: Mapping[str, PositionView],
    tolerance: Decimal = DEFAULT_TOLERANCE,
) -> ReconcileResult:
    local = {position.symbol: position for position in local_positions}
    problems: list[str] = []
    details: dict[str, dict[str, str]] = {}

    for symbol in sorted(set(local) | set(broker_positions)):
        local_qty = local[symbol].qty if symbol in local else ZERO
        broker_qty = broker_positions[symbol].qty if symbol in broker_positions else ZERO
        if abs(local_qty - broker_qty) <= tolerance:
            continue
        details[symbol] = {"local": str(local_qty), "broker": str(broker_qty)}
        if local_qty == ZERO:
            problems.append(f"{symbol}: 브로커에만 {broker_qty}")
        elif broker_qty == ZERO:
            problems.append(f"{symbol}: 로컬에만 {local_qty}")
        else:
            problems.append(f"{symbol}: 로컬 {local_qty} vs 브로커 {broker_qty}")

    return ReconcileResult(ok=not problems, mismatches=tuple(problems), details=details)


def is_reconciled_today(value: str | None, today: str) -> bool:
    return value == today


def now_iso(now: datetime) -> str:
    return now.isoformat()
