"""슬롯 경쟁 — 점수 = 알파 가중치 x (|낙폭| / |진입 임계|)."""

from __future__ import annotations

from typing import Iterable, Sequence

from ..config import Universe
from .entry import EntryEvaluation


def rank_candidates(
    evaluations: Iterable[EntryEvaluation],
    universe: Universe,
) -> list[EntryEvaluation]:
    """허용된 후보만 점수 내림차순. 동점은 알파 가중치 -> 심볼명 순(결정적)."""
    allowed = [item for item in evaluations if item.allowed]
    return sorted(
        allowed,
        key=lambda item: (-item.score, -universe.alpha(item.symbol), item.symbol),
    )


def select_for_slots(
    evaluations: Iterable[EntryEvaluation],
    universe: Universe,
    free_slots: int,
) -> list[EntryEvaluation]:
    if free_slots <= 0:
        return []
    return rank_candidates(evaluations, universe)[:free_slots]


def free_slots(total_slots: int, open_positions: Sequence[object]) -> int:
    """빈 슬롯 수. 물타기는 슬롯을 추가로 쓰지 않는다."""
    return max(0, total_slots - len(open_positions))
