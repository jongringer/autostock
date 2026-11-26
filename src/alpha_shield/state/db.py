"""SQLite 상태 저장 (WAL)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    symbol            TEXT PRIMARY KEY,
    qty               TEXT NOT NULL,
    avg_price         TEXT NOT NULL,
    entry_at          TEXT NOT NULL,
    first_entry_date  TEXT NOT NULL,
    entry_vix         TEXT,
    tp_target_pct     TEXT NOT NULL,
    add_count         INTEGER NOT NULL DEFAULT 0,
    last_add_at       TEXT
);

CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    score         TEXT,
    threshold_pct TEXT,
    status        TEXT NOT NULL DEFAULT 'PENDING'
);

CREATE TABLE IF NOT EXISTS cooldowns (
    symbol    TEXT PRIMARY KEY,
    until_at  TEXT NOT NULL,
    reason    TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    broker_order_id TEXT,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    mode            TEXT NOT NULL,
    amount_usd      TEXT,
    qty             TEXT,
    status          TEXT NOT NULL,
    filled_qty      TEXT NOT NULL DEFAULT '0',
    avg_price       TEXT,
    filled_amount   TEXT,
    commission      TEXT NOT NULL DEFAULT '0',
    tax             TEXT NOT NULL DEFAULT '0',
    reason          TEXT,
    error_code      TEXT,
    message         TEXT,
    created_at      TEXT NOT NULL,
    filled_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS day_state (
    trade_date          TEXT PRIMARY KEY,
    equity_open         TEXT NOT NULL,
    buys_halted_reason  TEXT,
    realized_pnl        TEXT NOT NULL DEFAULT '0',
    updated_at          TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def initialize(self) -> None:
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()
