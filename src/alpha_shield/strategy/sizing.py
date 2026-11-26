"""자금·슬롯 사이징 — 2슬롯 고정, 현금 버퍼, 최소 주문, 종목 상한."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..config import CapitalConfig
from ..models import Position
from ..numeric import ZERO, money_down, qty6


@dataclass(frozen=True)
class Tier:
    slot_amount: Decimal
    symbol_cap: Decimal


def tier_for(equity: Decimal, capital: CapitalConfig) -> Tier:
    entry, cap = capital.tier(equity)
    return Tier(slot_amount=entry, symbol_cap=cap)


def cash_buffer(equity: Decimal, capital: CapitalConfig) -> Decimal:
    return capital.cash_buffer(equity)


def spendable_cash(equity: Decimal, cash: Decimal, capital: CapitalConfig) -> Decimal:
    """현금 안전 버퍼를 뺀, 주문에 쓸 수 있는 금액."""
    available = cash - cash_buffer(equity, capital)
    return available if available > ZERO else ZERO


class Budget:
    """한 사이클 안에서 매수 예산을 순차적으로 소진한다."""

    def __init__(self, amount: Decimal) -> None:
        self.remaining = amount if amount > ZERO else ZERO

    def take(self, amount: Decimal) -> Decimal:
        granted = min(amount, self.remaining)
        self.remaining -= granted
        return granted


def plan_new_entry(
    equity: Decimal,
    cash: Decimal,
    capital: CapitalConfig,
    budget: Budget,
) -> Decimal | None:
    """신규 진입 금액 (USD, 소수 2자리 내림). 최소 주문 미만이면 None."""
    tier = tier_for(equity, capital)
    available = spendable_cash(equity, cash, capital)
    amount = min(tier.slot_amount, available, budget.remaining)
    amount = money_down(amount)
    if amount < capital.min_order_usd:
        return None
    return budget.take(amount)


def plan_add(
    position: Position,
    last_price: Decimal,
    equity: Decimal,
    cash: Decimal,
    capital: CapitalConfig,
    budget: Budget,
) -> Decimal | None:
    """물타기 금액. 종목 노출 상한($170/$240) 안에서만."""
    tier = tier_for(equity, capital)
    exposure = position.qty * last_price
    room_by_cap = tier.symbol_cap - exposure
    available = spendable_cash(equity, cash, capital)
    amount = min(tier.slot_amount, room_by_cap, available, budget.remaining)
    amount = money_down(amount)
    if amount < capital.min_order_usd:
        return None
    return budget.take(amount)


def buy_quantity_for_amount(amount: Decimal, price: Decimal) -> Decimal:
    """금액 주문의 예상 수량 (소수 6자리 내림) — 원장 표시·검증용."""
    if price <= ZERO:
        return ZERO
    return qty6(amount / price)


def exposure_after(position: Position | None, last_price: Decimal, add_amount: Decimal | None = None) -> Decimal:
    base = ZERO if position is None else position.qty * last_price
    return base if add_amount is None else base + add_amount
