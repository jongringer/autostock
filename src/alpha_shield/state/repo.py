"""상태 저장소 — 포지션·신호·쿨다운·주문 원장·일일 상태."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, Mapping

from ..models import OrderRequest, OrderResult, Position
from ..numeric import ZERO, dec
from .db import SCHEMA, Database


@dataclass(frozen=True)
class SignalRecord:
    id: int
    symbol: str
    created_at: datetime
    expires_at: datetime
    score: Decimal | None
    threshold_pct: Decimal | None
    status: str


@dataclass(frozen=True)
class OrderRecord:
    client_order_id: str
    broker_order_id: str | None
    symbol: str
    side: str
    mode: str
    amount_usd: Decimal | None
    qty: Decimal | None
    status: str
    filled_qty: Decimal
    avg_price: Decimal | None
    filled_amount: Decimal | None
    commission: Decimal
    tax: Decimal
    reason: str
    error_code: str | None
    message: str
    created_at: datetime
    filled_at: datetime | None


@dataclass(frozen=True)
class DayState:
    trade_date: date
    equity_open: Decimal
    buys_halted_reason: str | None
    realized_pnl: Decimal
    updated_at: datetime


def _dt(value: str | None) -> datetime | None:
    return None if not value else datetime.fromisoformat(value)


class Repo:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.conn = database.connect()
        # 초기화가 안 된 DB 로 들어와도 안전하게
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------------ 포지션
    def positions(self) -> list[Position]:
        rows = self.conn.execute("SELECT * FROM positions ORDER BY symbol").fetchall()
        return [self._position_from_row(row) for row in rows]

    def position(self, symbol: str) -> Position | None:
        row = self.conn.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()
        return None if row is None else self._position_from_row(row)

    @staticmethod
    def _position_from_row(row) -> Position:
        return Position(
            symbol=row["symbol"],
            qty=dec(row["qty"]),
            avg_price=dec(row["avg_price"]),
            entry_at=datetime.fromisoformat(row["entry_at"]),
            first_entry_date=date.fromisoformat(row["first_entry_date"]),
            entry_vix=None if row["entry_vix"] is None else dec(row["entry_vix"]),
            tp_target_pct=dec(row["tp_target_pct"]),
            add_count=int(row["add_count"]),
            last_add_at=None if row["last_add_at"] is None else datetime.fromisoformat(row["last_add_at"]),
        )

    def save_position(self, position: Position) -> None:
        self.conn.execute(
            """
            INSERT INTO positions (symbol, qty, avg_price, entry_at, first_entry_date, entry_vix,
                                   tp_target_pct, add_count, last_add_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                qty=excluded.qty, avg_price=excluded.avg_price, entry_at=excluded.entry_at,
                first_entry_date=excluded.first_entry_date, entry_vix=excluded.entry_vix,
                tp_target_pct=excluded.tp_target_pct, add_count=excluded.add_count,
                last_add_at=excluded.last_add_at
            """,
            (
                position.symbol,
                str(position.qty),
                str(position.avg_price),
                position.entry_at.isoformat(),
                position.first_entry_date.isoformat(),
                None if position.entry_vix is None else str(position.entry_vix),
                str(position.tp_target_pct),
                position.add_count,
                None if position.last_add_at is None else position.last_add_at.isoformat(),
            ),
        )

    def delete_position(self, symbol: str) -> None:
        self.conn.execute("DELETE FROM positions WHERE symbol=?", (symbol,))

    # ------------------------------------------------------------------ 신호
    def add_signal(
        self,
        symbol: str,
        created_at: datetime,
        expires_at: datetime,
        score: Decimal | None,
        threshold_pct: Decimal | None,
    ) -> int:
        cursor = self.conn.execute(
            "INSERT INTO signals (symbol, created_at, expires_at, score, threshold_pct, status)"
            " VALUES (?, ?, ?, ?, ?, 'PENDING')",
            (
                symbol,
                created_at.isoformat(),
                expires_at.isoformat(),
                None if score is None else str(score),
                None if threshold_pct is None else str(threshold_pct),
            ),
        )
        return int(cursor.lastrowid)

    def signals(self, status: str = "PENDING") -> list[SignalRecord]:
        rows = self.conn.execute(
            "SELECT * FROM signals WHERE status=? ORDER BY created_at", (status,)
        ).fetchall()
        return [
            SignalRecord(
                id=int(row["id"]),
                symbol=row["symbol"],
                created_at=datetime.fromisoformat(row["created_at"]),
                expires_at=datetime.fromisoformat(row["expires_at"]),
                score=None if row["score"] is None else dec(row["score"]),
                threshold_pct=None if row["threshold_pct"] is None else dec(row["threshold_pct"]),
                status=row["status"],
            )
            for row in rows
        ]

    def set_signal_status(self, signal_id: int, status: str) -> None:
        self.conn.execute("UPDATE signals SET status=? WHERE id=?", (status, signal_id))

    def latest_signal(self, symbol: str) -> SignalRecord | None:
        row = self.conn.execute(
            "SELECT * FROM signals WHERE symbol=? ORDER BY id DESC LIMIT 1", (symbol,)
        ).fetchone()
        if row is None:
            return None
        return SignalRecord(
            id=int(row["id"]),
            symbol=row["symbol"],
            created_at=datetime.fromisoformat(row["created_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
            score=None if row["score"] is None else dec(row["score"]),
            threshold_pct=None if row["threshold_pct"] is None else dec(row["threshold_pct"]),
            status=row["status"],
        )

    # ------------------------------------------------------------------ 쿨다운
    def set_cooldown(self, symbol: str, until_at: datetime, reason: str = "exit") -> None:
        self.conn.execute(
            "INSERT INTO cooldowns (symbol, until_at, reason) VALUES (?, ?, ?)"
            " ON CONFLICT(symbol) DO UPDATE SET until_at=excluded.until_at, reason=excluded.reason",
            (symbol, until_at.isoformat(), reason),
        )

    def cooldown_until(self, symbol: str) -> datetime | None:
        row = self.conn.execute("SELECT until_at FROM cooldowns WHERE symbol=?", (symbol,)).fetchone()
        return None if row is None else datetime.fromisoformat(row["until_at"])

    def cooldown_reason(self, symbol: str) -> str | None:
        row = self.conn.execute("SELECT reason FROM cooldowns WHERE symbol=?", (symbol,)).fetchone()
        return None if row is None else row["reason"]

    def clear_cooldown(self, symbol: str) -> None:
        self.conn.execute("DELETE FROM cooldowns WHERE symbol=?", (symbol,))

    # ------------------------------------------------------------------ 주문 원장
    def record_order(
        self,
        request: OrderRequest,
        result: OrderResult,
        *,
        mode: str,
        created_at: datetime,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO orders (client_order_id, broker_order_id, symbol, side, mode, amount_usd, qty,
                                status, filled_qty, avg_price, filled_amount, commission, tax, reason,
                                error_code, message, created_at, filled_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_order_id) DO UPDATE SET
                broker_order_id=excluded.broker_order_id, status=excluded.status,
                filled_qty=excluded.filled_qty, avg_price=excluded.avg_price,
                filled_amount=excluded.filled_amount, commission=excluded.commission,
                tax=excluded.tax, error_code=excluded.error_code, message=excluded.message,
                filled_at=excluded.filled_at
            """,
            (
                request.client_order_id,
                result.broker_order_id,
                request.symbol,
                request.side,
                mode,
                None if request.amount_usd is None else str(request.amount_usd),
                None if request.qty is None else str(request.qty),
                result.status,
                str(result.filled_qty),
                None if result.avg_price is None else str(result.avg_price),
                None if result.filled_amount is None else str(result.filled_amount),
                str(result.commission),
                str(result.tax),
                request.reason,
                result.error_code,
                result.message,
                created_at.isoformat(),
                None if result.filled_at is None else result.filled_at.isoformat(),
            ),
        )

    def order_by_client_id(self, client_order_id: str) -> OrderRecord | None:
        row = self.conn.execute(
            "SELECT * FROM orders WHERE client_order_id=?", (client_order_id,)
        ).fetchone()
        return None if row is None else self._order_from_row(row)

    @staticmethod
    def _order_from_row(row) -> OrderRecord:
        return OrderRecord(
            client_order_id=row["client_order_id"],
            broker_order_id=row["broker_order_id"],
            symbol=row["symbol"],
            side=row["side"],
            mode=row["mode"],
            amount_usd=None if row["amount_usd"] is None else dec(row["amount_usd"]),
            qty=None if row["qty"] is None else dec(row["qty"]),
            status=row["status"],
            filled_qty=dec(row["filled_qty"]),
            avg_price=None if row["avg_price"] is None else dec(row["avg_price"]),
            filled_amount=None if row["filled_amount"] is None else dec(row["filled_amount"]),
            commission=dec(row["commission"]),
            tax=dec(row["tax"]),
            reason=row["reason"] or "",
            error_code=row["error_code"],
            message=row["message"] or "",
            created_at=datetime.fromisoformat(row["created_at"]),
            filled_at=None if row["filled_at"] is None else datetime.fromisoformat(row["filled_at"]),
        )

    def orders_since(self, since: datetime) -> list[OrderRecord]:
        rows = self.conn.execute(
            "SELECT * FROM orders WHERE created_at >= ? ORDER BY created_at", (since.isoformat(),)
        ).fetchall()
        return [self._order_from_row(row) for row in rows]

    def orders_between(self, start: datetime, end: datetime) -> list[OrderRecord]:
        rows = self.conn.execute(
            "SELECT * FROM orders WHERE created_at >= ? AND created_at < ? ORDER BY created_at",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        return [self._order_from_row(row) for row in rows]

    def pending_orders(self) -> list[OrderRecord]:
        rows = self.conn.execute(
            "SELECT * FROM orders WHERE status IN ('PENDING','PARTIAL_FILLED','PENDING_CANCEL','PENDING_REPLACE')"
            " ORDER BY created_at"
        ).fetchall()
        return [self._order_from_row(row) for row in rows]

    def filled_orders(self, start: datetime | None = None, end: datetime | None = None) -> list[OrderRecord]:
        sql = "SELECT * FROM orders WHERE status IN ('FILLED','PARTIAL_FILLED')"
        params: list[str] = []
        if start is not None:
            sql += " AND created_at >= ?"
            params.append(start.isoformat())
        if end is not None:
            sql += " AND created_at < ?"
            params.append(end.isoformat())
        sql += " ORDER BY created_at"
        rows = self.conn.execute(sql, params).fetchall()
        return [self._order_from_row(row) for row in rows]

    def count_orders(self, since: datetime, symbol: str | None = None, side: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM orders WHERE created_at >= ?"
        params: list[str] = [since.isoformat()]
        if symbol is not None:
            sql += " AND symbol=?"
            params.append(symbol)
        if side is not None:
            sql += " AND side=?"
            params.append(side)
        row = self.conn.execute(sql, params).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------ kv / 일일 상태
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return default if row is None else row["value"]

    def kv_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def kv_get_json(self, key: str, default: object | None = None) -> object | None:
        raw = self.kv_get(key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except ValueError:
            return default

    def kv_set_json(self, key: str, value: object) -> None:
        self.kv_set(key, json.dumps(value, ensure_ascii=False))

    def day_state(self, trade_date: date) -> DayState | None:
        row = self.conn.execute(
            "SELECT * FROM day_state WHERE trade_date=?", (trade_date.isoformat(),)
        ).fetchone()
        if row is None:
            return None
        return DayState(
            trade_date=date.fromisoformat(row["trade_date"]),
            equity_open=dec(row["equity_open"]),
            buys_halted_reason=row["buys_halted_reason"],
            realized_pnl=dec(row["realized_pnl"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def ensure_day_state(self, trade_date: date, equity_open: Decimal, now: datetime) -> DayState:
        existing = self.day_state(trade_date)
        if existing is not None:
            return existing
        self.conn.execute(
            "INSERT INTO day_state (trade_date, equity_open, buys_halted_reason, realized_pnl, updated_at)"
            " VALUES (?, ?, NULL, '0', ?)",
            (trade_date.isoformat(), str(equity_open), now.isoformat()),
        )
        return DayState(trade_date, equity_open, None, ZERO, now)

    def halt_buys(self, trade_date: date, reason: str, now: datetime) -> None:
        self.conn.execute(
            "UPDATE day_state SET buys_halted_reason=?, updated_at=? WHERE trade_date=?",
            (reason, now.isoformat(), trade_date.isoformat()),
        )

    def add_realized_pnl(self, trade_date: date, amount: Decimal, now: datetime) -> None:
        state = self.day_state(trade_date)
        current = state.realized_pnl if state is not None else ZERO
        self.conn.execute(
            "UPDATE day_state SET realized_pnl=?, updated_at=? WHERE trade_date=?",
            (str(current + amount), now.isoformat(), trade_date.isoformat()),
        )

    # ------------------------------------------------------------------ 유틸
    def bulk(self, statements: Iterable[tuple[str, tuple[object, ...]]]) -> None:
        for sql, params in statements:
            self.conn.execute(sql, params)

    def table_counts(self) -> Mapping[str, int]:
        out = {}
        for table in ("positions", "signals", "cooldowns", "orders", "day_state"):
            row = self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
            out[table] = int(row["n"])
        return out
