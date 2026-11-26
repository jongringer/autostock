"""시장 가드 — 나스닥 선물(NQ) -1.5% 이하 또는 VIX 30 이상이면 신규 매수 전면 차단."""

from __future__ import annotations

import logging
from decimal import Decimal

from ..config import MarketGuardConfig
from ..models import GuardDecision

LOG = logging.getLogger(__name__)


class MarketGuard:
    def __init__(self, market_data, config: MarketGuardConfig) -> None:
        self.market_data = market_data
        self.config = config

    def check(self) -> GuardDecision:
        nq: Decimal | None = None
        vix: Decimal | None = None
        try:
            nq = self.market_data.futures_change_pct(self.config.nq_symbol)
        except Exception as exc:  # noqa: BLE001 - 데이터 실패는 fail-closed
            LOG.warning("NQ 조회 실패: %s", exc)
        try:
            vix = self.market_data.last_price(self.config.vix_symbol)
            if vix is None:
                vix_close = self.market_data.last_daily_close(self.config.vix_symbol)
                vix = None if vix_close is None else vix_close[0]
        except Exception as exc:  # noqa: BLE001
            LOG.warning("VIX 조회 실패: %s", exc)

        detail = {
            "nq_symbol": self.config.nq_symbol,
            "nq_change_pct": None if nq is None else str(nq),
            "vix": None if vix is None else str(vix),
        }
        if nq is None or vix is None:
            if self.config.fail_closed:
                return GuardDecision.block("market-data-unavailable", **detail)
            return GuardDecision.ok(detail)
        if nq <= -self.config.nq_block_pct:
            return GuardDecision.block("nq-crash", **detail)
        if vix >= self.config.vix_block:
            return GuardDecision.block("vix-panic", **detail)
        return GuardDecision.ok(detail)
