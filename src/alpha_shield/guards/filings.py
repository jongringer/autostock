"""공시 가드 — SEC 8-K 구조적 악재."""

from __future__ import annotations

import logging
from datetime import date
from typing import Iterable, Mapping

from ..config import FilingsGuardConfig
from ..models import GuardDecision

LOG = logging.getLogger(__name__)


class FilingsGuard:
    def __init__(self, provider, config: FilingsGuardConfig) -> None:
        self.provider = provider
        self.config = config

    def decisions(self, symbols: Iterable[str], today: date) -> Mapping[str, GuardDecision]:
        out: dict[str, GuardDecision] = {}
        for symbol in symbols:
            cik = self.config.cik.get(symbol)
            if cik is None:
                out[symbol] = GuardDecision.ok(skip="no-cik")
                continue
            check = self.provider.check(
                symbol,
                cik,
                self.config.block_keywords,
                today,
                lookback_days=self.config.lookback_days,
            )
            if not check.checked:
                if self.config.block_on_error:
                    out[symbol] = GuardDecision.block(check.reason, symbol=symbol)
                else:
                    out[symbol] = GuardDecision.ok()
                continue
            if check.blocked:
                out[symbol] = GuardDecision.block(check.reason, **dict(check.detail))
            else:
                out[symbol] = GuardDecision.ok(**dict(check.detail))
        return out
