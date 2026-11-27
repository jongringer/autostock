"""전 계층이 공유하는 값 객체."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Mapping

from .numeric import ZERO, dec

Side = Literal["BUY", "SELL"]

ORDER_STATUS_FILLED = "FILLED"
ORDER_STATUS_PARTIAL = "PARTIAL_FILLED"
ORDER_STATUS_PENDING = "PENDING"
ORDER_STATUS_REJECTED = "REJECTED"
ORDER_STATUS_CANCELED = "CANCELED"
ORDER_STATUS_DRY_RUN = "DRY_RUN"
OPEN_ORDER_STATUSES = frozenset({"PENDING", "PARTIAL_FILLED", "PENDING_CANCEL", "PENDING_REPLACE"})
TERMINAL_ORDER_STATUSES = frozenset(
    {"FILLED", "CANCELED", "REJECTED", "CANCEL_REJECTED", "REPLACE_REJECTED", "REPLACED"}
)


@dataclass(frozen=True)
class Bar:
    """OHLCV 한 봉. timestamp 는 봉의 기준 시각(일봉=현지 자정, 1분봉=봉 종료)."""

    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = ZERO

    @property
    def typical(self) -> Decimal:
        return (self.high + self.low + self.close) / Decimal("3")


@dataclass(frozen=True)
class Quote:
    symbol: str
    last_price: Decimal
    timestamp: datetime | None = None
    currency: str = "USD"

    def age_seconds(self, now: datetime) -> int | None:
        if self.timestamp is None:
            return None
        delta = now - self.timestamp
        return int(delta.total_seconds())


@dataclass(frozen=True)
class OrderRequest:
    """브로커 중립 주문 요청.

    - 매수(금액): amount_usd 지정 (토스 `orderAmount`, US MARKET 전용)
    - 매도(수량): qty 지정 (토스 `quantity`, 소수 6자리)
    - 매수(수량·지정가): qty + limit_price (정수주 모드에서만, v3.3 기본 경로 아님)
    """

    symbol: str
    side: Side
    client_order_id: str
    amount_usd: Decimal | None = None
    qty: Decimal | None = None
    limit_price: Decimal | None = None
    reason: str = ""
    ref_price: Decimal | None = None  # 페이퍼 체결 기준가(테스트·시뮬레이션)

    def __post_init__(self) -> None:
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"invalid side: {self.side!r}")
        if self.amount_usd is None and self.qty is None:
            raise ValueError("amount_usd 또는 qty 중 하나는 필요합니다")
        if self.amount_usd is not None and self.qty is not None:
            raise ValueError("amount_usd 와 qty 는 동시에 지정할 수 없습니다")
        if not self.client_order_id:
            raise ValueError("client_order_id 는 필수입니다 (멱등키)")
        if len(self.client_order_id) > 36:
            raise ValueError("client_order_id 는 36자 이하여야 합니다")


@dataclass(frozen=True)
class OrderResult:
    client_order_id: str
    status: str
    broker_order_id: str | None = None
    filled_qty: Decimal = ZERO
    avg_price: Decimal | None = None
    filled_amount: Decimal | None = None
    commission: Decimal = ZERO
    tax: Decimal = ZERO
    filled_at: datetime | None = None
    error_code: str | None = None
    message: str = ""

    @property
    def is_filled(self) -> bool:
        return self.status == ORDER_STATUS_FILLED and self.filled_qty > ZERO

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_ORDER_STATUSES or self.status == ORDER_STATUS_DRY_RUN


@dataclass(frozen=True)
class Position:
    """로컬 원장의 보유 포지션. 브로커 보유와 대조(reconcile)한다."""

    symbol: str
    qty: Decimal
    avg_price: Decimal
    entry_at: datetime
    first_entry_date: date
    tp_target_pct: Decimal
    entry_vix: Decimal | None = None
    add_count: int = 0
    last_add_at: datetime | None = None

    @property
    def cost_basis(self) -> Decimal:
        return self.qty * self.avg_price

    def exposure(self, last_price: Decimal) -> Decimal:
        return self.qty * last_price

    def as_row(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "qty": str(self.qty),
            "avg_price": str(self.avg_price),
            "entry_at": self.entry_at.isoformat(),
            "first_entry_date": self.first_entry_date.isoformat(),
            "entry_vix": None if self.entry_vix is None else str(self.entry_vix),
            "tp_target_pct": str(self.tp_target_pct),
            "add_count": self.add_count,
            "last_add_at": None if self.last_add_at is None else self.last_add_at.isoformat(),
        }


@dataclass(frozen=True)
class PositionView:
    """브로커가 보고한 보유 종목."""

    symbol: str
    qty: Decimal
    avg_price: Decimal
    last_price: Decimal | None = None
    currency: str = "USD"

    def market_value(self) -> Decimal:
        price = self.last_price if self.last_price is not None else self.avg_price
        return self.qty * price


@dataclass(frozen=True)
class AccountState:
    cash_usd: Decimal
    equity_usd: Decimal
    positions: Mapping[str, PositionView] = field(default_factory=dict)
    as_of: datetime | None = None

    def exposure(self) -> Decimal:
        return sum((view.market_value() for view in self.positions.values()), ZERO)


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    reason: str = ""
    detail: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def ok(cls, detail: Mapping[str, object] | None = None, **extra: object) -> "GuardDecision":
        merged: dict[str, object] = dict(detail or {})
        merged.update(extra)
        return cls(True, "", merged)

    @classmethod
    def block(cls, reason: str, detail: Mapping[str, object] | None = None, **extra: object) -> "GuardDecision":
        merged: dict[str, object] = dict(detail or {})
        merged.update(extra)
        return cls(False, reason, merged)


@dataclass(frozen=True)
class CycleReport:
    """한 사이클의 결과 요약 (로그·테스트·알림에 사용)."""

    at: datetime
    skipped: str | None = None
    equity_usd: Decimal = ZERO
    cash_usd: Decimal = ZERO
    sells: tuple[OrderResult, ...] = ()
    buys: tuple[OrderResult, ...] = ()
    blocked: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def orders(self) -> tuple[OrderResult, ...]:
        return self.sells + self.buys


def money_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def parse_decimal(value: object | None) -> Decimal | None:
    return None if value is None else dec(value)
