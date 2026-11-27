"""계좌 규정 가드 — 현금 계좌 미결제 자금(GFV) · 마진 계좌 PDT.

- 현금 계좌: 매도대금은 T+1 에 결제된다. 결제 전 대금으로 다시 사면 GFV(Good Faith
  Violation) 가 나므로, 결제 대기 금액을 빼고 사이징하며 동일 종목 재매수도 막는다.
- 마진 계좌: 자산 $25,000 미만에서 5영업일 내 '당일 매수·매도' 3회에 도달하면 그날
  신규 진입을 중단한다(Pattern Day Trader 보호).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Sequence

from ..calendar import MarketCalendar
from ..config import AccountGuardConfig
from ..models import GuardDecision
from ..numeric import ZERO, dec
from ..state.repo import Repo

LOG = logging.getLogger(__name__)

KV_UNSETTLED = "unsettled_proceeds"
KV_DAY_TRADES = "day_trades"


@dataclass(frozen=True)
class UnsettledProceeds:
    symbol: str
    amount: Decimal
    sold_on: date
    settles_on: date

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "amount": str(self.amount),
            "sold_on": self.sold_on.isoformat(),
            "settles_on": self.settles_on.isoformat(),
        }


def next_business_day(calendar: MarketCalendar, day: date) -> date:
    cursor = day + timedelta(days=1)
    for _ in range(10):
        if calendar.is_trading_day(cursor):
            return cursor
        cursor += timedelta(days=1)
    return cursor


class AccountRegulationGuard:
    def __init__(self, repo: Repo, config: AccountGuardConfig, calendar: MarketCalendar) -> None:
        self.repo = repo
        self.config = config
        self.calendar = calendar

    # ------------------------------------------------------------------ 미결제 자금
    def _unsettled(self) -> list[UnsettledProceeds]:
        raw = self.repo.kv_get_json(KV_UNSETTLED) or []
        out: list[UnsettledProceeds] = []
        for item in raw if isinstance(raw, list) else []:
            try:
                out.append(
                    UnsettledProceeds(
                        symbol=str(item["symbol"]),
                        amount=dec(item["amount"]),
                        sold_on=date.fromisoformat(item["sold_on"]),
                        settles_on=date.fromisoformat(item["settles_on"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def _save(self, items: Sequence[UnsettledProceeds]) -> None:
        self.repo.kv_set_json(KV_UNSETTLED, [item.to_dict() for item in items])

    def prune(self, today: date) -> list[UnsettledProceeds]:
        """결제일이 지난 기록은 지운다."""
        items = [item for item in self._unsettled() if item.settles_on > today]
        self._save(items)
        return items

    def record_sell(self, symbol: str, proceeds: Decimal, today: date) -> None:
        """현금 계좌 매도대금을 결제 대기로 기록한다. 마진 계좌는 즉시 결제로 본다."""
        if not self.config.is_cash or proceeds <= ZERO:
            return
        settles = today
        for _ in range(max(1, self.config.settlement_business_days)):
            settles = next_business_day(self.calendar, settles)
        items = self.prune(today)
        items.append(UnsettledProceeds(symbol, proceeds, today, settles))
        self._save(items)

    def unsettled_total(self, today: date) -> Decimal:
        if not self.config.is_cash:
            return ZERO
        return sum((item.amount for item in self.prune(today)), ZERO)

    def settled_cash(self, cash: Decimal, today: date) -> Decimal:
        """매수 예산으로 쓸 수 있는 결제 완료 현금."""
        if not self.config.is_cash:
            return cash
        return cash - self.unsettled_total(today)

    def rebuy_blocked(self, symbol: str, today: date) -> bool:
        if not self.config.is_cash or not self.config.unsettled_rebuy_block:
            return False
        return any(item.symbol == symbol for item in self.prune(today))

    # ------------------------------------------------------------------ PDT
    def _day_trades(self) -> list[dict[str, str]]:
        raw = self.repo.kv_get_json(KV_DAY_TRADES) or []
        return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    def record_day_trade(self, symbol: str, today: date, now: datetime) -> None:
        """당일 매수·매도가 같은 날 성립한 경우(데이트레이드)를 기록한다."""
        items = [
            item
            for item in self._day_trades()
            if self._within_window(date.fromisoformat(item["date"]), today)
        ]
        items.append({"symbol": symbol, "date": today.isoformat(), "at": now.isoformat()})
        self.repo.kv_set_json(KV_DAY_TRADES, items)

    def _within_window(self, day: date, today: date) -> bool:
        if day > today:
            return False
        return self.calendar.trading_days_between(day, today) < self.config.pdt_window_business_days

    def day_trade_count(self, today: date) -> int:
        return len([item for item in self._day_trades() if self._within_window(date.fromisoformat(item["date"]), today)])

    def pdt_status(self, today: date, equity: Decimal) -> GuardDecision:
        if self.config.settlement != "margin":
            return GuardDecision.ok(applies=False)
        if equity >= self.config.pdt_min_equity_usd:
            return GuardDecision.ok(applies=False)
        count = self.day_trade_count(today)
        detail = {"day_trades": count, "limit": self.config.pdt_day_trade_limit}
        if count >= self.config.pdt_day_trade_limit:
            return GuardDecision.block("pdt-limit", **detail)
        return GuardDecision.ok(applies=True, **detail)

    # ------------------------------------------------------------------ 요약
    def describe(self, today: date, equity: Decimal) -> dict[str, object]:
        unsettled = self.unsettled_total(today)
        return {
            "settlement": self.config.settlement,
            "unsettled_usd": str(unsettled),
            "unsettled_symbols": sorted({item.symbol for item in self.prune(today)}),
            "day_trades": self.day_trade_count(today),
            "day_trade_limit": self.config.pdt_day_trade_limit,
            "pdt_applies": self.config.settlement == "margin" and equity < self.config.pdt_min_equity_usd,
        }
