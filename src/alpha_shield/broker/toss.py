"""토스증권 브로커 어댑터.

공식 스펙(research/toss.json) 준수:
- 금액 기반 시장가 매수: `orderAmount` (USD 소수 2자리) — US MARKET 전용
- 소수점 수량 시장가 매도: `quantity` (소수 6자리) — MARKET+SELL 전용
- `clientOrderId` 멱등키 (10분, ≤36자)
- 금액·소수점 주문 접수 창: 정규장 시작 ~ 종료 1시간 전(15:00 ET)
- 계좌 헤더 `X-Tossinvest-Account` 필수
- 실주문은 `LIVE_ORDERS=true` + `state/ARMED` 둘 다 있을 때만
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from decimal import Decimal
from typing import Callable, Mapping

from ..clock import now_et, parse_iso
from ..models import (
    ORDER_STATUS_FILLED,
    ORDER_STATUS_PARTIAL,
    ORDER_STATUS_PENDING,
    ORDER_STATUS_REJECTED,
    AccountState,
    OrderRequest,
    OrderResult,
    PositionView,
)
from ..numeric import ZERO, dec, money_down, qty6
from ..safety import SafetyLock
from ..marketdata.toss import TossApiError, TossClient
from .base import OrderRejected

LOG = logging.getLogger(__name__)

FINAL_ORDER_STATUSES = {"FILLED", "CANCELED", "REJECTED", "CANCEL_REJECTED", "REPLACE_REJECTED", "REPLACED"}


class TossBroker:
    name = "live"

    def __init__(
        self,
        client: TossClient,
        safety: SafetyLock,
        *,
        min_order_usd: Decimal = Decimal("10.00"),
        session_provider: Callable[[datetime], object | None] | None = None,
        fractional_close_buffer_min: int = 60,
        clock: Callable[[], datetime] = now_et,
        sleep: Callable[[float], None] = time.sleep,
        fill_poll_attempts: int = 2,
        fill_poll_interval: float = 1.0,
    ) -> None:
        self.client = client
        self.safety = safety
        self.min_order_usd = min_order_usd
        self.session_provider = session_provider
        self.fractional_close_buffer_min = fractional_close_buffer_min
        self._clock = clock
        self._sleep = sleep
        self.fill_poll_attempts = fill_poll_attempts
        self.fill_poll_interval = fill_poll_interval
        self._orders: dict[str, OrderResult] = {}

    # ------------------------------------------------------------------ 계좌
    def account(self) -> AccountState:
        cash_payload = self.client.get("/api/v1/buying-power", params={"currency": "USD"}, needs_account=True)
        cash = dec((cash_payload or {}).get("cashBuyingPower", "0"))
        holdings = self.client.get("/api/v1/holdings", needs_account=True) or {}
        views: dict[str, PositionView] = {}
        market_value = ZERO
        for item in holdings.get("items") or []:
            if str(item.get("currency", "")).upper() != "USD":
                continue
            try:
                view = PositionView(
                    symbol=str(item["symbol"]).upper(),
                    qty=dec(item["quantity"]),
                    avg_price=dec(item["averagePurchasePrice"]),
                    last_price=dec(item["lastPrice"]) if item.get("lastPrice") else None,
                    currency="USD",
                )
            except (KeyError, TypeError, ValueError) as exc:
                LOG.warning("토스 보유 종목 파싱 실패 %s: %s", item, exc)
                continue
            views[view.symbol] = view
            market_value += view.market_value()
        return AccountState(
            cash_usd=cash,
            equity_usd=cash + market_value,
            positions=views,
            as_of=self._clock(),
        )

    # ------------------------------------------------------------------ 주문
    def _assert_fractional_window(self, now: datetime) -> None:
        if self.session_provider is None:
            return
        try:
            window = self.session_provider(now)
        except Exception as exc:  # noqa: BLE001 - 세션 확인 실패는 주문 거부
            raise OrderRejected("session-unavailable", f"세션 확인 실패: {exc}") from exc
        if window is None:
            raise OrderRejected("order-hours-closed", "휴장일에는 주문할 수 없습니다")
        if not window.fractional_sell_allowed(now, self.fractional_close_buffer_min):
            raise OrderRejected(
                "amount-order-outside-regular-hours",
                "금액·소수점 주문은 정규장 시작 ~ 종료 1시간 전(15:00 ET)까지만 접수됩니다",
            )

    def _build_payload(self, request: OrderRequest) -> dict[str, object]:
        payload: dict[str, object] = {
            "clientOrderId": request.client_order_id,
            "symbol": request.symbol.upper(),
            "side": request.side,
            "orderType": "MARKET",
        }
        if request.side == "BUY":
            if request.amount_usd is None:
                raise OrderRejected("invalid-request", "v3.2 매수는 금액(orderAmount) 기반입니다")
            amount = money_down(request.amount_usd)
            if amount < self.min_order_usd:
                raise OrderRejected("min-order-amount", f"최소 주문 금액 ${self.min_order_usd} 미만입니다")
            payload["orderAmount"] = str(amount)
        else:
            if request.qty is None or request.qty <= ZERO:
                raise OrderRejected("invalid-request", "매도 수량이 없습니다")
            payload["quantity"] = str(qty6(request.qty))
        return payload

    def place_order(self, request: OrderRequest) -> OrderResult:
        self.safety.assert_live()
        now = self._clock()
        try:
            self._assert_fractional_window(now)
            payload = self._build_payload(request)
        except OrderRejected as exc:
            LOG.warning("토스 주문 사전 검증 거부 %s: %s", exc.code, exc.message)
            return OrderResult(
                client_order_id=request.client_order_id,
                status=ORDER_STATUS_REJECTED,
                error_code=exc.code,
                message=exc.message,
                filled_at=now,
            )
        try:
            response = self._submit(payload)
        except TossApiError as exc:
            LOG.warning("토스 주문 거부 %s %s: %s", request.symbol, exc.code, exc.message)
            return OrderResult(
                client_order_id=request.client_order_id,
                status=ORDER_STATUS_REJECTED,
                error_code=exc.code,
                message=exc.message,
                filled_at=now,
            )
        order_id = str((response or {}).get("orderId", ""))
        if not order_id:
            return OrderResult(
                client_order_id=request.client_order_id,
                status=ORDER_STATUS_REJECTED,
                error_code="missing-order-id",
                message="주문 응답에 orderId 가 없습니다",
                filled_at=now,
            )
        return self._sync(order_id, request.client_order_id)

    def _submit(self, payload: Mapping[str, object]) -> object:
        last_error: TossApiError | None = None
        for attempt in range(2):  # 409 request-in-progress 는 같은 키로 재시도
            try:
                return self.client.post("/api/v1/orders", json_body=payload, needs_account=True)
            except TossApiError as exc:
                if exc.code == "request-in-progress" and attempt == 0:
                    last_error = exc
                    self._sleep(self.fill_poll_interval)
                    continue
                raise
        if last_error is not None:  # pragma: no cover
            raise last_error
        raise OrderRejected("order-submit-failed", "주문 전송 실패")  # pragma: no cover

    def _sync(self, order_id: str, client_order_id: str) -> OrderResult:
        result: OrderResult | None = None
        for attempt in range(self.fill_poll_attempts):
            detail = self.client.get(f"/api/v1/orders/{order_id}", needs_account=True)
            result = self._parse_order(detail or {}, client_order_id)
            if result.status in FINAL_ORDER_STATUSES:
                return result
            if attempt + 1 < self.fill_poll_attempts:
                self._sleep(self.fill_poll_interval)
        assert result is not None
        return result

    @staticmethod
    def _parse_order(order: Mapping, client_order_id: str) -> OrderResult:
        execution = order.get("execution") or {}
        status = str(order.get("status", ORDER_STATUS_PENDING))
        mapped = {
            "FILLED": ORDER_STATUS_FILLED,
            "PARTIAL_FILLED": ORDER_STATUS_PARTIAL,
        }.get(status, status)
        filled = dec(execution.get("filledQuantity") or 0)
        filled_at = execution.get("filledAt")
        return OrderResult(
            client_order_id=client_order_id,
            status=mapped,
            broker_order_id=str(order.get("orderId", "")) or None,
            filled_qty=filled,
            avg_price=dec(execution["averageFilledPrice"]) if execution.get("averageFilledPrice") else None,
            filled_amount=dec(execution["filledAmount"]) if execution.get("filledAmount") else None,
            commission=dec(execution.get("commission") or 0),
            tax=dec(execution.get("tax") or 0),
            filled_at=parse_iso(filled_at) if filled_at else None,
            message=str(order.get("rejectReason", "") or ""),
        )

    def get_order(self, broker_order_id: str) -> OrderResult | None:
        cached = self._orders.get(broker_order_id)
        if cached is not None:
            return cached
        try:
            detail = self.client.get(f"/api/v1/orders/{broker_order_id}", needs_account=True)
        except TossApiError as exc:
            LOG.warning("주문 조회 실패 %s: %s", broker_order_id, exc)
            return None
        result = self._parse_order(detail or {}, "")
        self._orders[broker_order_id] = result
        return result

    def update_prices(self, prices: Mapping[str, object]) -> None:
        return None
