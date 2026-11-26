import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from helpers import d, et

from alpha_shield.state.db import Database
from alpha_shield.state.repo import Repo
from alpha_shield.strategy.signals import SignalBook


class SignalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(Database(Path(self.tmp.name) / "test.db"))
        self.book = SignalBook(self.repo, d("25"))
        self.now = et(2026, 12, 1, 10, 30)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_publish_creates_pending_signal(self):
        record = self.book.publish("NVDA", self.now, 900, d("4.5"), d("1.04"))
        self.assertEqual(record.symbol, "NVDA")
        self.assertEqual(record.expires_at, self.now + timedelta(seconds=900))
        self.assertEqual(len(self.book.valid(self.now)), 1)

    def test_publish_reuses_pending_without_extending(self):
        first = self.book.publish("NVDA", self.now, 900, d("4.5"), d("1.04"))
        second = self.book.publish("NVDA", self.now + timedelta(seconds=300), 900, d("5.0"), d("1.04"))
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.expires_at, first.expires_at)
        self.assertEqual(len(self.repo.signals()), 1)

    def test_signal_expires_after_900_seconds(self):
        self.book.publish("NVDA", self.now, 900, d("4.5"), d("1.04"))
        self.assertEqual(self.book.valid(self.now + timedelta(seconds=899)), self.book.valid(self.now))
        changes = self.book.housekeeping(self.now + timedelta(seconds=901), {})
        self.assertEqual(changes, [("NVDA", "EXPIRED")])
        self.assertEqual(self.book.valid(self.now + timedelta(seconds=901)), [])

    def test_redelivery_discards_when_rsi_recovers(self):
        self.book.publish("NVDA", self.now, 900, d("4.5"), d("1.04"))
        changes = self.book.housekeeping(self.now + timedelta(seconds=60), {"NVDA": d("26")})
        self.assertEqual(changes, [("NVDA", "DISCARDED")])
        self.assertEqual(len(self.repo.signals("DISCARDED")), 1)
        self.assertEqual(self.book.valid(self.now), [])

    def test_rsi_exactly_25_is_kept(self):
        self.book.publish("NVDA", self.now, 900, d("4.5"), d("1.04"))
        changes = self.book.housekeeping(self.now + timedelta(seconds=60), {"NVDA": d("25")})
        self.assertEqual(changes, [])
        self.assertEqual(len(self.book.valid(self.now)), 1)

    def test_new_signal_after_discard(self):
        self.book.publish("NVDA", self.now, 900, d("4.5"), d("1.04"))
        self.book.housekeeping(self.now + timedelta(seconds=60), {"NVDA": d("40")})
        again = self.book.publish("NVDA", self.now + timedelta(seconds=300), 900, d("6"), d("1.04"))
        self.assertEqual(len(self.repo.signals("PENDING")), 1)
        self.assertNotEqual(again.id, 1)

    def test_consume(self):
        record = self.book.publish("NVDA", self.now, 900, d("4.5"), d("1.04"))
        self.book.consume(record.id)
        self.assertEqual(self.book.valid(self.now), [])


if __name__ == "__main__":
    unittest.main()
