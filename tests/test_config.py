import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import yaml

from helpers import ROOT

from alpha_shield.config import (
    ConfigError,
    load_all,
    load_guards,
    load_settings,
    load_strategy,
)


class ConfigTests(unittest.TestCase):
    def test_load_real_config(self):
        settings, strategy, guards = load_all(root=ROOT)
        self.assertEqual(settings.normalized_mode, "dry-run")
        self.assertEqual(len(strategy.universe.weights), 8)
        self.assertEqual(strategy.universe.alpha("NVDA"), Decimal("1.5"))
        self.assertEqual(strategy.capital.slots, 2)
        self.assertEqual(strategy.entry.signal_ttl_seconds, 900)
        self.assertEqual(guards.market.vix_block, Decimal("30.0"))
        self.assertIn("NVDA", guards.filings.cik)

    def test_unknown_mode_rejected(self):
        with self.assertRaises(ConfigError):
            load_settings(env={"ALPHA_MODE": "turbo"}, root=ROOT)

    def test_slots_must_be_two(self):
        raw = yaml.safe_load((ROOT / "config" / "strategy.yaml").read_text(encoding="utf-8"))
        raw["capital"]["slots"] = 3
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "strategy.yaml"
            path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "2슬롯"):
                load_strategy(path)

    def test_missing_key_rejected(self):
        raw = yaml.safe_load((ROOT / "config" / "strategy.yaml").read_text(encoding="utf-8"))
        del raw["exit"]["stop_loss_pct"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "strategy.yaml"
            path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "stop_loss_pct"):
                load_strategy(path)

    def test_signal_ttl_must_be_900(self):
        raw = yaml.safe_load((ROOT / "config" / "strategy.yaml").read_text(encoding="utf-8"))
        raw["entry"]["signal_ttl_seconds"] = 600
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "strategy.yaml"
            path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "900"):
                load_strategy(path)

    def test_guards_keywords_lowercased(self):
        guards = load_guards(ROOT / "config" / "guards.yaml")
        self.assertIn("bankruptcy", guards.filings.block_keywords)
        self.assertTrue(guards.news.enabled)

    def test_settings_env(self):
        settings = load_settings(
            env={
                "ALPHA_MODE": "live",
                "LIVE_ORDERS": "true",
                "TOSS_ACCOUNT_SEQ": "7",
                "ALPHA_DATA_DIR": "/tmp/as-state",
                "TOSS_CLIENT_ID": "cid",
            },
            root=ROOT,
        )
        self.assertEqual(settings.normalized_mode, "live")
        self.assertTrue(settings.live_orders_env)
        self.assertEqual(settings.toss_account_seq, 7)
        self.assertEqual(settings.armed_file, Path("/tmp/as-state/ARMED"))
        self.assertNotIn("client_secret", settings.as_safe_dict())

    def test_macro_calendar_dates_are_valid(self):
        raw = json.loads((ROOT / "config" / "macro_calendar_2026.json").read_text(encoding="utf-8"))
        self.assertEqual(len(raw["events"]["FOMC"]), 8)
        self.assertEqual(len(raw["events"]["CPI"]), 12)
        self.assertEqual(len(raw["events"]["NFP"]), 12)
        self.assertIn("2026-12-09", raw["events"]["FOMC"])


if __name__ == "__main__":
    unittest.main()
