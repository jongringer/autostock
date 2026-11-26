"""거래 원장 — 세무·검증용 CSV 내보내기 (체결가·수량·수수료·환율 열 포함)."""

from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from ..clock import KST, to_kst
from ..state.repo import OrderRecord, Repo

LEDGER_COLUMNS = [
    "filled_at_kst",
    "symbol",
    "side",
    "quantity",
    "price",
    "amount",
    "commission",
    "tax",
    "currency",
    "fx_rate_krw",
    "reason",
    "client_order_id",
    "broker_order_id",
    "mode",
]


def _month_bounds(month: str) -> tuple[datetime, datetime]:
    year, mon = (int(part) for part in month.split("-"))
    start = datetime(year, mon, 1, tzinfo=KST)
    if mon == 12:
        end = datetime(year + 1, 1, 1, tzinfo=KST)
    else:
        end = datetime(year, mon + 1, 1, tzinfo=KST)
    return start, end


def export_ledger(repo: Repo, out_dir: Path | str, month: str | None = None) -> Path:
    """체결 주문을 CSV 로 내보낸다. month='YYYY-MM' 이면 그 달만."""
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    if month:
        start, end = _month_bounds(month)
        rows = repo.filled_orders(start, end)
        filename = f"ledger-{month}.csv"
    else:
        rows = repo.filled_orders()
        filename = f"ledger-all-{date.today().isoformat()}.csv"
    path = out_path / filename
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=LEDGER_COLUMNS)
        writer.writeheader()
        for record in rows:
            writer.writerow(_row(record))
    return path


def _row(record: OrderRecord) -> dict[str, str]:
    filled_at = record.filled_at or record.created_at
    return {
        "filled_at_kst": to_kst(filled_at).strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": record.symbol,
        "side": record.side,
        "quantity": str(record.filled_qty),
        "price": "" if record.avg_price is None else str(record.avg_price),
        "amount": "" if record.filled_amount is None else str(record.filled_amount),
        "commission": str(record.commission),
        "tax": str(record.tax),
        "currency": "USD",
        "fx_rate_krw": "",  # 체결 시점 환율은 토스 exchange-rate 로 별도 보강 가능
        "reason": record.reason,
        "client_order_id": record.client_order_id,
        "broker_order_id": record.broker_order_id or "",
        "mode": record.mode,
    }


def summary(repo: Repo, day: date) -> dict[str, Decimal | int | str]:
    start = datetime.combine(day, datetime.min.time(), tzinfo=KST)
    end = start.replace(hour=23, minute=59, second=59)
    rows = repo.filled_orders(start, end)
    buys = sum((row.filled_amount or Decimal("0")) for row in rows if row.side == "BUY")
    sells = sum((row.filled_amount or Decimal("0")) for row in rows if row.side == "SELL")
    fees = sum((row.commission + row.tax for row in rows), Decimal("0"))
    return {
        "trade_date": day.isoformat(),
        "fills": len(rows),
        "buy_amount": buys,
        "sell_amount": sells,
        "fees": fees,
    }
