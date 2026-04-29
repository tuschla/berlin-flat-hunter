"""Tests for BerlinHunter — focused on _record_health and pipeline wiring"""
import os
import re
import tempfile
import unittest
from unittest.mock import MagicMock

import requests.exceptions

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


class TestDriverRecycle(unittest.TestCase):
    """Verify per-cycle Selenium driver recycling on wedge/dead session.

    Flathunter's upstream try_crawl only catches CaptchaUnsolvableError +
    requests.RequestException — Selenium errors propagate, leaving the dead
    driver attached to the searcher for the *next* cycle. BerlinHunter
    overrides crawl_for_exposes to null searcher.driver on wedge so the
    next get_driver() call rebuilds a fresh instance.
    """

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self._tmpdir, "db.sqlite")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_hunter(self, searchers, target_urls):
        cfg = _make_config(target_urls=target_urls, searchers=searchers, db_path=self.db_path)
        hunter = BerlinHunter.__new__(BerlinHunter)
        hunter.config = cfg
        hunter.berlin_config = cfg
        hunter.id_watch = MagicMock()
        from berlin_flat_hunter.monitoring.schema_monitor import SchemaMonitor
        hunter.schema_monitor = SchemaMonitor(
            os.path.join(self._tmpdir, "monitor.json"), {}
        )
        hunter._driver_last_recycled = {}
        return hunter

    def _webdriver_searcher(self, name: str, url_pattern: str):
        """Mock searcher that mimics WebdriverCrawler — has a .driver attribute
        that the recycle path will null on wedge."""
        from flathunter.webdriver_crawler import WebdriverCrawler
        s = MagicMock(spec=WebdriverCrawler)
        s.get_name.return_value = name
        s.URL_PATTERN = re.compile(url_pattern)
        s.driver = MagicMock()  # initial 'live' driver
        return s

    def test_webdriver_exception_nulls_driver(self):
        """On WebDriverException, searcher.driver must be set to None so the
        next cycle's get_driver() reconstructs a fresh one."""
        from selenium.common.exceptions import WebDriverException
        searcher = self._webdriver_searcher("Kleinanzeigen", r"https://kleinanzeigen\.de")
        searcher.crawl.side_effect = WebDriverException("session deleted")
        hunter = self._make_hunter([searcher], ["https://kleinanzeigen.de/foo"])
        results = list(hunter.crawl_for_exposes())
        self.assertEqual(results, [])
        self.assertIsNone(searcher.driver)
        searcher.driver.quit if False else None  # satisfy lint

    def test_urllib3_max_retry_nulls_driver(self):
        """urllib3.MaxRetryError (sibling of requests.ConnectionError, not subclass)
        also indicates a wedged session and must trigger recycle."""
        import urllib3.exceptions
        searcher = self._webdriver_searcher("Kleinanzeigen", r"https://kleinanzeigen\.de")
        searcher.crawl.side_effect = urllib3.exceptions.MaxRetryError(
            None, "http://localhost", "boom"  # type: ignore[arg-type]
        )
        hunter = self._make_hunter([searcher], ["https://kleinanzeigen.de/foo"])
        list(hunter.crawl_for_exposes())
        self.assertIsNone(searcher.driver)

    def test_request_exception_does_not_recycle(self):
        """A plain requests.RequestException is not a Selenium wedge — leave
        the driver alone, just skip the URL for this cycle."""
        searcher = self._webdriver_searcher("Kleinanzeigen", r"https://kleinanzeigen\.de")
        live_driver = searcher.driver
        searcher.crawl.side_effect = requests.exceptions.ConnectionError("dns")
        hunter = self._make_hunter([searcher], ["https://kleinanzeigen.de/foo"])
        list(hunter.crawl_for_exposes())
        self.assertIs(searcher.driver, live_driver)

    def test_healthy_crawler_keeps_driver(self):
        searcher = self._webdriver_searcher("Kleinanzeigen", r"https://kleinanzeigen\.de")
        live_driver = searcher.driver
        searcher.crawl.return_value = [{"crawler": "Kleinanzeigen", "title": "X"}]
        hunter = self._make_hunter([searcher], ["https://kleinanzeigen.de/foo"])
        results = list(hunter.crawl_for_exposes())
        self.assertEqual(len(results), 1)
        self.assertIs(searcher.driver, live_driver)

    def test_probe_recycles_dead_driver_pre_crawl(self):
        """A driver that died between cycles is detected via the cheap
        current_url probe at the start of the cycle — before the first crawl."""
        from selenium.common.exceptions import WebDriverException

        class _DeadDriver:
            """Real object whose current_url access raises like a dead chromedriver."""
            @property
            def current_url(self):  # noqa: D401
                raise WebDriverException("disconnected")
            def quit(self):  # selenium contract for tear-down
                return None

        searcher = self._webdriver_searcher("Kleinanzeigen", r"https://kleinanzeigen\.de")
        searcher.driver = _DeadDriver()
        searcher.crawl.return_value = []
        hunter = self._make_hunter([searcher], ["https://kleinanzeigen.de/foo"])
        list(hunter.crawl_for_exposes())
        self.assertIsNone(searcher.driver)

    def test_recycle_rate_limited_when_not_forced(self):
        """The non-forced recycle path (probe failure) must not tear down
        more than once per _DRIVER_RECYCLE_MIN_INTERVAL seconds."""
        searcher = self._webdriver_searcher("Kleinanzeigen", r"https://kleinanzeigen\.de")
        hunter = self._make_hunter([searcher], ["https://kleinanzeigen.de/foo"])
        # Mark a recent recycle so the rate limiter rejects the next non-forced one
        import time as time_mod
        hunter._driver_last_recycled["Kleinanzeigen"] = time_mod.monotonic()
        original_driver = searcher.driver
        hunter._recycle_driver(searcher, force=False)
        # Within the window; driver must not be torn down
        self.assertIs(searcher.driver, original_driver)
        # Forced path bypasses the rate limiter (a wedge already happened)
        hunter._recycle_driver(searcher, force=True)
        self.assertIsNone(searcher.driver)

    def test_other_searchers_continue_after_one_wedges(self):
        """One wedged crawler must not stop others from running."""
        from selenium.common.exceptions import WebDriverException
        ka = self._webdriver_searcher("Kleinanzeigen", r"https://kleinanzeigen\.de")
        ka.crawl.side_effect = WebDriverException("dead")
        gewobag = _make_searcher("Gewobag", r"https://www\.gewobag\.de")
        gewobag.crawl.return_value = [{"crawler": "Gewobag", "title": "Y"}]
        hunter = self._make_hunter(
            [ka, gewobag],
            ["https://kleinanzeigen.de/x", "https://www.gewobag.de/y"],
        )
        results = list(hunter.crawl_for_exposes())
        # Gewobag's result survives the KA wedge
        self.assertEqual([r["title"] for r in results], ["Y"])
        self.assertIsNone(ka.driver)


class TestTotalFailure(unittest.TestCase):
    """A hunt cycle that throws before _record_health runs must still tick
    consecutive_empty for every configured crawler — otherwise the schema
    monitor stays silent forever on a fully-broken pipeline."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self._tmpdir, "db.sqlite")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_hunter(self, searchers, target_urls):
        cfg = _make_config(target_urls=target_urls, searchers=searchers, db_path=self.db_path)
        hunter = BerlinHunter.__new__(BerlinHunter)
        hunter.config = cfg
        hunter.berlin_config = cfg
        hunter.id_watch = MagicMock()
        from berlin_flat_hunter.monitoring.schema_monitor import SchemaMonitor
        hunter.schema_monitor = SchemaMonitor(
            os.path.join(self._tmpdir, "monitor.json"), {}
        )
        hunter._alert_notifiers = []
        hunter._driver_last_recycled = {}
        return hunter

    def test_inner_exception_records_all_configured_empty(self):
        gewobag = _make_searcher("Gewobag", r"https://www\.gewobag\.de")
        wbm = _make_searcher("Wbm", r"https://www\.wbm\.de")
        immowelt = _make_searcher("Immowelt", r"https://www\.immowelt\.de")
        hunter = self._make_hunter(
            [gewobag, wbm, immowelt],
            target_urls=["https://www.gewobag.de/x", "https://www.wbm.de/y"],
        )
        # Force _hunt_flats_inner to blow up
        hunter._hunt_flats_inner = MagicMock(side_effect=RuntimeError("DB lost"))
        result = hunter.hunt_flats()
        self.assertEqual(result, [])
        health = hunter.schema_monitor.get_health_summary()
        self.assertEqual(health["Gewobag"]["consecutive_empty"], 1)
        self.assertEqual(health["Wbm"]["consecutive_empty"], 1)
        # Immowelt had no matching URL → not configured → not ticked
        self.assertNotIn("Immowelt", health)

    def test_inner_exception_dispatches_alert_after_threshold(self):
        gewobag = _make_searcher("Gewobag", r"https://www\.gewobag\.de")
        hunter = self._make_hunter(
            [gewobag], target_urls=["https://www.gewobag.de/x"],
        )
        notifier = MagicMock()
        hunter._alert_notifiers = [notifier]
        hunter._hunt_flats_inner = MagicMock(side_effect=RuntimeError("dead"))
        # Default threshold is 3 consecutive empties → 3 cycles trigger alert
        for _ in range(3):
            hunter.hunt_flats()
        self.assertTrue(notifier.notify.called)


if __name__ == "__main__":
    unittest.main()
