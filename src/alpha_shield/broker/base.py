"""브로커 인터페이스 — 전략·가드·원장은 브로커 비의존."""

from __future__ import annotations

from typing import Mapping, Protocol

from ..models import AccountState, OrderRequest, OrderResult


class BrokerError(RuntimeError):
    pass


class OrderRejected(BrokerError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


class Broker(Protocol):
    name: str

    def account(self) -> AccountState: ...

    def place_order(self, request: OrderRequest) -> OrderResult: ...

    def get_order(self, broker_order_id: str) -> OrderResult | None: ...

    def update_prices(self, prices: Mapping[str, object]) -> None:
        """페이퍼 모드에서 평가금액 계산용. 실브로커는 무시한다."""
