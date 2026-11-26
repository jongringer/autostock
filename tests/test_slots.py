import unittest

from helpers import ROOT, d

from alpha_shield.config import load_strategy
from alpha_shield.strategy.entry import EntryEvaluation
from alpha_shield.strategy.slots import free_slots, rank_candidates, select_for_slots


def evaluation(symbol, score, allowed=True):
    return EntryEvaluation(
        symbol=symbol,
        allowed=allowed,
        reason="ok" if allowed else "no-dip",
        reasons=() if allowed else ("no-dip",),
        drawdown_pct=d("-2"),
        threshold_pct=d("1"),
        score=d(score),
    )


class SlotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.universe = load_strategy(ROOT / "config" / "strategy.yaml").universe

    def test_rank_by_score_desc(self):
        ranked = rank_candidates([evaluation("AAPL", "2.1"), evaluation("NVDA", "5.2"), evaluation("TSLA", "3.3")], self.universe)
        self.assertEqual([item.symbol for item in ranked], ["NVDA", "TSLA", "AAPL"])

    def test_rank_excludes_blocked(self):
        ranked = rank_candidates([evaluation("NVDA", "5.2", allowed=False), evaluation("AAPL", "2.1")], self.universe)
        self.assertEqual([item.symbol for item in ranked], ["AAPL"])

    def test_tie_break_by_alpha_weight_then_symbol(self):
        ranked = rank_candidates([evaluation("AAPL", "3"), evaluation("NVDA", "3"), evaluation("GOOGL", "3")], self.universe)
        self.assertEqual([item.symbol for item in ranked], ["NVDA", "AAPL", "GOOGL"])

    def test_select_top_for_free_slots(self):
        candidates = [evaluation("AAPL", "1"), evaluation("TSLA", "9"), evaluation("AMZN", "7")]
        selected = select_for_slots(candidates, self.universe, 2)
        self.assertEqual([item.symbol for item in selected], ["TSLA", "AMZN"])

    def test_no_slots(self):
        self.assertEqual(select_for_slots([evaluation("TSLA", "9")], self.universe, 0), [])

    def test_free_slots(self):
        self.assertEqual(free_slots(2, []), 2)
        self.assertEqual(free_slots(2, [object(), object()]), 0)
        self.assertEqual(free_slots(2, [object(), object(), object()]), 0)


if __name__ == "__main__":
    unittest.main()
