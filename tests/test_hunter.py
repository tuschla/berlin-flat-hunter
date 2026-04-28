"""Tests for BerlinHunter — focused on _record_health and pipeline wiring"""
import os
import re
import tempfile
import unittest
from unittest.mock import MagicMock

from berlin_flat_hunter.hunter import BerlinHunter


def _make_searcher(name: str, url_pattern: str):
    s = MagicMock()
    s.get_name.return_value = name
    s.URL_PATTERN = re.compile(url_pattern)
    return s


def _make_config(target_urls=None, searchers=None, db_path=None, **flags):
    cfg = MagicMock()
    cfg.config = {}
    cfg.target_urls.return_value = target_urls or []
    cfg.searchers.return_value = searchers or []
    cfg.database_location.return_value = db_path or "/tmp/test_hunter.db"
    cfg.polygon_filter_enabled.return_value = flags.get("polygon", False)
    cfg.plz_filter_enabled.return_value = flags.get("plz", False)
    cfg.ollama_enabled.return_value = flags.get("ollama", False)
    cfg.auto_apply_enabled.return_value = flags.get("apply", False)
    cfg.stats_enabled.return_value = flags.get("stats", False)
    cfg.stats_db_path.return_value = ""
    return cfg


class TestRecordHealth(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self._tmpdir, "db.sqlite")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_hunter(self, target_urls, searchers):
        cfg = _make_config(target_urls=target_urls, searchers=searchers, db_path=self.db_path)
        # Bypass flathunter Hunter.__init__ check that requires YamlConfig
        hunter = BerlinHunter.__new__(BerlinHunter)
        hunter.config = cfg
        hunter.berlin_config = cfg
        hunter.id_watch = MagicMock()
        from berlin_flat_hunter.monitoring.schema_monitor import SchemaMonitor
        hunter.schema_monitor = SchemaMonitor(
            os.path.join(self._tmpdir, "monitor.json"), {}
        )
        return hunter

    def test_skips_crawlers_with_no_matching_urls(self):
        """Unused crawlers (no matching URL) must NOT be tracked as empty crawls."""
        gewobag = _make_searcher("Gewobag", r"https://www\.gewobag\.de")
        immowelt = _make_searcher("Immowelt", r"https://www\.immowelt\.de")
        hunter = self._make_hunter(
            target_urls=["https://www.gewobag.de/foo"],
            searchers=[gewobag, immowelt],
        )
        hunter._record_health([])
        health = hunter.schema_monitor.get_health_summary()
        # Gewobag had matching URL but no results → tracked as empty
        self.assertIn("Gewobag", health)
        self.assertEqual(health["Gewobag"]["consecutive_empty"], 1)
        # Immowelt had no matching URL → must NOT be in health state
        self.assertNotIn("Immowelt", health)

    def test_results_reset_empty_counter(self):
        gewobag = _make_searcher("Gewobag", r"https://www\.gewobag\.de")
        hunter = self._make_hunter(
            target_urls=["https://www.gewobag.de/foo"],
            searchers=[gewobag],
        )
        hunter._record_health([])
        hunter._record_health([{"crawler": "Gewobag", "title": "X", "url": "u", "address": "a", "price": "p"}])
        health = hunter.schema_monitor.get_health_summary()
        self.assertEqual(health["Gewobag"]["consecutive_empty"], 0)

    def test_crawler_with_results_not_double_counted(self):
        """A crawler with results must not also get record_empty_crawl called."""
        gewobag = _make_searcher("Gewobag", r"https://www\.gewobag\.de")
        hunter = self._make_hunter(
            target_urls=["https://www.gewobag.de/foo"],
            searchers=[gewobag],
        )
        # Spy on record_empty_crawl
        empty_spy = MagicMock()
        hunter.schema_monitor.record_empty_crawl = empty_spy
        hunter._record_health([
            {"crawler": "Gewobag", "title": "X", "url": "u", "address": "a", "price": "p"}
        ])
        empty_spy.assert_not_called()

    def test_alerts_routed_to_notifiers(self):
        """Schema alerts must be pushed through every configured notifier."""
        gewobag = _make_searcher("Gewobag", r"https://www\.gewobag\.de")
        hunter = self._make_hunter(
            target_urls=["https://www.gewobag.de/foo"],
            searchers=[gewobag],
        )
        # Force the schema monitor to emit an alert
        hunter.schema_monitor.record_crawl = MagicMock(return_value=["[SCHEMA ALERT] test"])
        hunter.schema_monitor.record_empty_crawl = MagicMock(return_value=[])
        notifier_a, notifier_b = MagicMock(), MagicMock()
        hunter._alert_notifiers = [notifier_a, notifier_b]
        hunter._record_health([])
        notifier_a.notify.assert_called_once_with("[SCHEMA ALERT] test")
        notifier_b.notify.assert_called_once_with("[SCHEMA ALERT] test")

    def test_no_alerts_no_notifier_calls(self):
        gewobag = _make_searcher("Gewobag", r"https://www\.gewobag\.de")
        hunter = self._make_hunter(
            target_urls=["https://www.gewobag.de/foo"],
            searchers=[gewobag],
        )
        hunter.schema_monitor.record_crawl = MagicMock(return_value=[])
        hunter.schema_monitor.record_empty_crawl = MagicMock(return_value=[])
        notifier = MagicMock()
        hunter._alert_notifiers = [notifier]
        hunter._record_health([])
        notifier.notify.assert_not_called()

    def test_notifier_exception_does_not_break_loop(self):
        gewobag = _make_searcher("Gewobag", r"https://www\.gewobag\.de")
        hunter = self._make_hunter(
            target_urls=["https://www.gewobag.de/foo"],
            searchers=[gewobag],
        )
        hunter.schema_monitor.record_crawl = MagicMock(return_value=["alert"])
        hunter.schema_monitor.record_empty_crawl = MagicMock(return_value=[])
        bad = MagicMock()
        bad.notify.side_effect = RuntimeError("send failed")
        good = MagicMock()
        hunter._alert_notifiers = [bad, good]
        hunter._record_health([])  # must not raise
        good.notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
