"""내장 모의(paper) 브로커 — 시장가 체결 가정, 슬리피지·수수료 반영.

실제 돈이 오가지 않는다. 기본 실행 모드는 dry-run 이고, paper 는 이 브로커를 쓴다.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Protocol

from ..clock import now_et
from ..models import (
    ORDER_STATUS_FILLED,
    ORDER_STATUS_REJECTED,
    AccountState,
    OrderRequest,
    OrderResult,
    PositionView,
)
from ..numeric import ONE, ZERO, dec, money, qty6
from .base import OrderRejected


class PaperSnapshotStore(Protocol):
    def load(self) -> dict | None: ...

    def save(self, snapshot: dict) -> None: ...


class JsonFileSnapshotStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
    def load(self) -> dict | None:
        import json

        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def save(self, snapshot: dict) -> None:
        import json

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")


class RepoPaperStore:
    """SQLite kv 에 페이퍼 계좌 스냅샷을 보관한다 (재시작 후 이어서)."""

    def __init__(self, repo) -> None:
        self.repo = repo

    def load(self) -> dict | None:
        value = self.repo.kv_get_json("paper_state")
        return value if isinstance(value, dict) else None

    def save(self, snapshot: dict) -> None:
        self.repo.kv_set_json("paper_state", snapshot)


class PaperBroker:
    name = "paper"

    def __init__(
        self,
        starting_cash: Decimal,
        *,
        slippage_bps: Decimal = Decimal("5"),
        commission_bps: Decimal = Decimal("10"),
        store: PaperSnapshotStore | None = None,
        clock=now_et,
    ) -> None:
        self.cash = starting_cash
        self.starting_cash = starting_cash
        self.slippage_bps = slippage_bps
        self.commission_bps = commission_bps
        self.store = store
        self._clock = clock
        self._positions: dict[str, dict[str, Decimal]] = {}
        self._prices: dict[str, Decimal] = {}
        self._orders: dict[str, OrderResult] = {}
        if store is not None:
            snapshot = store.load()
            if snapshot:
                self.restore(snapshot)

    # ------------------------------------------------------------------ 상태
    def snapshot(self) -> dict:
        return {
            "cash": str(self.cash),
            "positions": {
                symbol: {"qty": str(data["qty"]), "avg_price": str(data["avg_price"])}
                for symbol, data in self._positions.items()
            },
            "prices": {symbol: str(price) for symbol, price in self._prices.items()},
        }

    def restore(self, snapshot: Mapping) -> None:
        self.cash = dec(snapshot.get("cash", self.starting_cash))
        self._positions = {
            str(symbol): {"qty": dec(data["qty"]), "avg_price": dec(data["avg_price"])}
            for symbol, data in (snapshot.get("positions") or {}).items()
        }
        self._prices = {str(symbol): dec(price) for symbol, price in (snapshot.get("prices") or {}).items()}

    def _persist(self) -> None:
        if self.store is not None:
            self.store.save(self.snapshot())

    def update_prices(self, prices: Mapping[str, object]) -> None:
        for symbol, price in prices.items():
            value = price if isinstance(price, Decimal) else dec(price)
            if value > ZERO:
                self._prices[symbol] = value
        self._persist()

    def account(self) -> AccountState:
        views: dict[str, PositionView] = {}
        market_value = ZERO
        for symbol, data in self._positions.items():
            price = self._prices.get(symbol, data["avg_price"])
            view = PositionView(symbol=symbol, qty=data["qty"], avg_price=data["avg_price"], last_price=price)
            views[symbol] = view
            market_value += view.market_value()
        return AccountState(
            cash_usd=self.cash,
            equity_usd=self.cash + market_value,
            positions=views,
            as_of=self._clock(),
        )

    def _fill_price(self, request: OrderRequest, side: str) -> Decimal:
        reference = request.ref_price or self._prices.get(request.symbol)
        if reference is None or reference <= ZERO:
            raise OrderRejected("no-reference-price", f"{request.symbol} 체결 기준가가 없습니다")
        factor = ONE + (self.slippage_bps / Decimal(10000) * (ONE if side == "BUY" else -ONE))
        return reference * factor

    def place_order(self, request: OrderRequest) -> OrderResult:
        if request.side == "BUY":
            return self._buy(request)
        return self._sell(request)

    def _buy(self, request: OrderRequest) -> OrderResult:
        price = self._fill_price(request, "BUY")
        amount = money(request.amount_usd) if request.amount_usd is not None else None
        if amount is None:
            if request.qty is None:
                raise OrderRejected("invalid-request", "매수 수량 또는 금액이 없습니다")
            notional = request.qty * price
            quantity = qty6(request.qty)
        else:
            quantity = qty6(amount / price)
            notional = quantity * price
        commission = money(notional * self.commission_bps / Decimal(10000))
        total = notional + commission
        if quantity <= ZERO:
            return self._rejected(request, "invalid-quantity", "체결 수량이 0 입니다")
        if total > self.cash:
            return self._rejected(
                request,
                "insufficient-buying-power",
                f"현금 ${self.cash} < 필요 ${money(total)}",
            )
        self.cash -= total
        existing = self._positions.get(request.symbol)
        if existing:
            new_qty = existing["qty"] + quantity
            new_avg = (existing["qty"] * existing["avg_price"] + notional) / new_qty
            self._positions[request.symbol] = {"qty": new_qty, "avg_price": new_avg}
        else:
            self._positions[request.symbol] = {"qty": quantity, "avg_price": price}
        self._prices[request.symbol] = price
        result = OrderResult(
            client_order_id=request.client_order_id,
            status=ORDER_STATUS_FILLED,
            broker_order_id=f"paper-{uuid.uuid4().hex[:12]}",
            filled_qty=quantity,
            avg_price=money(price),
            filled_amount=money(notional),
            commission=commission,
            filled_at=self._clock(),
        )
        self._orders[result.broker_order_id] = result
        self._persist()
        return result

    def _sell(self, request: OrderRequest) -> OrderResult:
        held = self._positions.get(request.symbol)
        if not held or held["qty"] <= ZERO:
            return self._rejected(request, "insufficient-sellable-quantity", "보유 수량이 없습니다")
        quantity = qty6(request.qty if request.qty is not None else held["qty"])
        quantity = min(quantity, held["qty"])
        price = self._fill_price(request, "SELL")
        notional = quantity * price
        commission = money(notional * self.commission_bps / Decimal(10000))
        proceeds = notional - commission
        remaining = held["qty"] - quantity
        if remaining <= Decimal("0.000001"):
            self._positions.pop(request.symbol, None)
        else:
            self._positions[request.symbol] = {"qty": remaining, "avg_price": held["avg_price"]}
        self.cash += proceeds
        self._prices[request.symbol] = price
        result = OrderResult(
            client_order_id=request.client_order_id,
            status=ORDER_STATUS_FILLED,
            broker_order_id=f"paper-{uuid.uuid4().hex[:12]}",
            filled_qty=quantity,
            avg_price=money(price),
            filled_amount=money(notional),
            commission=commission,
            filled_at=self._clock(),
        )
        self._orders[result.broker_order_id] = result
        self._persist()
        return result

    def _rejected(self, request: OrderRequest, code: str, message: str) -> OrderResult:
        result = OrderResult(
            client_order_id=request.client_order_id,
            status=ORDER_STATUS_REJECTED,
            error_code=code,
            message=message,
            filled_at=self._clock(),
        )
        self._orders[result.client_order_id] = result
        self._persist()
        return result

    def get_order(self, broker_order_id: str) -> OrderResult | None:
        return self._orders.get(broker_order_id)

    def reset(self) -> None:
        self.cash = self.starting_cash
        self._positions.clear()
        self._prices.clear()
        self._persist()
