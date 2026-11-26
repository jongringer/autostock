import unittest
from decimal import Decimal

from helpers import d, make_intraday

from alpha_shield.indicators import (
    atr_pct,
    robust_intraday_high,
    sma,
    sma_series,
    true_ranges,
    wilder_atr,
    wilder_rsi,
)
from alpha_shield.models import Bar


class IndicatorTests(unittest.TestCase):
    def test_sma_basic(self):
        self.assertEqual(sma([d(1), d(2), d(3), d(4), d(5)], 3), Decimal("4"))
        self.assertIsNone(sma([d(1), d(2)], 3))

    def test_sma_series_length(self):
        series = sma_series([d(x) for x in range(1, 6)], 3)
        self.assertEqual(len(series), 5)
        self.assertEqual(series[2], Decimal("2"))
        self.assertEqual(series[-1], Decimal("4"))

    def test_rsi_known_value(self):
        # 10, 11, 10, 9 -> avg_gain 0.25, avg_loss 0.75 -> RS 1/3 -> RSI 25
        rsi = wilder_rsi([d(10), d(11), d(10), d(9)], 2)
        self.assertEqual(rsi, Decimal("25.0000"))

    def test_rsi_all_losses_is_zero(self):
        self.assertEqual(wilder_rsi([d(10), d(9), d(8), d(7)], 2), Decimal("0.0000"))

    def test_rsi_all_gains_is_hundred(self):
        self.assertEqual(wilder_rsi([d(1), d(2), d(3), d(4)], 2), Decimal("100.0000"))

    def test_rsi_requires_period_plus_one(self):
        self.assertIsNone(wilder_rsi([d(1), d(2)], 2))

    def test_rsi_is_decimal(self):
        value = wilder_rsi([d(x) for x in (10, 11, 10, 9)], 2)
        self.assertIsInstance(value, Decimal)

    def test_atr_constant_range(self):
        bars = [
            Bar(timestamp=b, open=d(10), high=d(11), low=d(9), close=d(10))
            for b in make_intraday([10, 10, 10], 10)[:3]
        ]
        self.assertEqual(wilder_atr(bars, 2), Decimal("2"))

    def test_atr_requires_period(self):
        bars = [Bar(timestamp=make_intraday([10], 10)[0].timestamp, open=d(10), high=d(11), low=d(9), close=d(10))]
        self.assertIsNone(wilder_atr(bars, 2))

    def test_atr_pct(self):
        bars = [
            Bar(timestamp=b, open=d(100), high=d(102), low=d(98), close=d(100))
            for b in make_intraday([100, 100, 100], 100)[:3]
        ]
        self.assertEqual(atr_pct(bars, 2), Decimal("4.0000"))

    def test_true_range_uses_previous_close(self):
        bars = [
            Bar(timestamp=make_intraday([10], 10)[0].timestamp, open=d(10), high=d(11), low=d(9), close=d(10)),
            Bar(timestamp=make_intraday([10], 10)[0].timestamp, open=d(11), high=d(12), low=d(10), close=d(11)),
        ]
        ranges = true_ranges(bars)
        self.assertEqual(ranges[0], Decimal("2"))
        self.assertEqual(ranges[1], Decimal("2"))

    def test_hod_filters_outlier_spike(self):
        bars = make_intraday([100, 101, 102], 101)
        spike = bars[1]
        bars.insert(
            2,
            Bar(timestamp=spike.timestamp, open=d(101), high=d(500), low=d(101), close=d(101)),
        )
        hod = robust_intraday_high(bars)
        self.assertEqual(hod, Decimal("102"))

    def test_hod_without_filter_keeps_spike(self):
        bars = make_intraday([100, 101, 500, 102], 101)
        self.assertEqual(robust_intraday_high(bars, enabled=False), Decimal("500"))

    def test_hod_empty(self):
        self.assertIsNone(robust_intraday_high([]))


if __name__ == "__main__":
    unittest.main()
