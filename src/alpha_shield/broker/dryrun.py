"""드라이런 브로커 — 주문을 전혀 보내지 않고 '보냈을 주문'만 기록한다."""

from __future__ import annotations

from decimal import Decimal
from typing import Mapping

from ..clock import now_et
from ..models import (
    ORDER_STATUS_DRY_RUN,
    AccountState,
    OrderRequest,
    OrderResult,
)
from ..numeric import ZERO


class DryRunBroker:
    name = "dry-run"

    def __init__(self, starting_cash: Decimal, clock=now_et) -> None:
        self.starting_cash = starting_cash
        self._clock = clock
        self.intents: list[OrderRequest] = []

    def account(self) -> AccountState:
        return AccountState(cash_usd=self.starting_cash, equity_usd=self.starting_cash, positions={}, as_of=self._clock())

    def place_order(self, request: OrderRequest) -> OrderResult:
        self.intents.append(request)
        return OrderResult(
            client_order_id=request.client_order_id,
            status=ORDER_STATUS_DRY_RUN,
            broker_order_id=None,
            filled_qty=ZERO,
            message="dry-run: 주문을 전송하지 않았습니다",
            filled_at=self._clock(),
        )

    def get_order(self, broker_order_id: str) -> OrderResult | None:
        return None

    def update_prices(self, prices: Mapping[str, object]) -> None:
        return None
