"""슬롯 경쟁 — 점수 = 알파 가중치 x min(|낙폭| / |진입 임계|, 2.5) (Hark 2).

가중치·상한 캡은 판정(`entry.evaluate_entry`)에서 이미 적용되므로, 여기서는
허용된 후보를 점수 내림차순으로 정렬하고 빈 슬롯만큼 고른다.
"""

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
