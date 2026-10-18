"""Tests for BerlinHunter — focused on _record_health and pipeline wiring"""
import os
import re
import tempfile
import unittest
from unittest.mock import MagicMock

import requests.exceptions
import requests_mock as req_mock
from flathunter.idmaintainer import IdMaintainer

from berlin_flat_hunter.config import BerlinConfig
from berlin_flat_hunter.hunter import BerlinHunter


class _FakeSearcher:
    """Plain (non-Selenium) crawler stand-in."""

    def __init__(self, name: str, url_pattern: str, items=None, exc=None):
        self._name = name
        self.URL_PATTERN = re.compile(url_pattern)
        self.items = items or []
        self.exc = exc

    def get_name(self):
        return self._name

    def crawl(self, url, max_pages=None):
        if self.exc is not None:
            raise self.exc
        return list(self.items)


class _Channel:
    """Alert channel stand-in (the `.notify(msg)` interface)."""

    def __init__(self, fail=False):
        self.messages: list[str] = []
        self.fail = fail

    def notify(self, message):
        self.messages.append(message)
        if self.fail:
            raise RuntimeError("send failed")


def _make_searcher(name: str, url_pattern: str):
    return _FakeSearcher(name, url_pattern)


def _build_hunter(tmpdir, target_urls, searchers, **extra) -> BerlinHunter:
    db = os.path.join(tmpdir, "db.sqlite")
    cfg = BerlinConfig({"database_location": db, "urls": target_urls, **extra})
    cfg.set_searchers(searchers)
    return BerlinHunter(cfg, IdMaintainer(db))


class TestRecordHealth(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self._tmpdir, "db.sqlite")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_hunter(self, target_urls, searchers):
        return _build_hunter(self._tmpdir, target_urls, searchers)

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
        channel_a, channel_b = _Channel(), _Channel()
        hunter._alert_notifiers = [channel_a, channel_b]
        hunter._record_health([])
        self.assertEqual(channel_a.messages, ["[SCHEMA ALERT] test"])
        self.assertEqual(channel_b.messages, ["[SCHEMA ALERT] test"])

    def test_no_alerts_no_notifier_calls(self):
        gewobag = _make_searcher("Gewobag", r"https://www\.gewobag\.de")
        hunter = self._make_hunter(
            target_urls=["https://www.gewobag.de/foo"],
            searchers=[gewobag],
        )
        hunter.schema_monitor.record_crawl = MagicMock(return_value=[])
        hunter.schema_monitor.record_empty_crawl = MagicMock(return_value=[])
        channel = _Channel()
        hunter._alert_notifiers = [channel]
        hunter._record_health([])
        self.assertEqual(channel.messages, [])

    def test_notifier_exception_does_not_break_loop(self):
        gewobag = _make_searcher("Gewobag", r"https://www\.gewobag\.de")
        hunter = self._make_hunter(
            target_urls=["https://www.gewobag.de/foo"],
            searchers=[gewobag],
        )
        hunter.schema_monitor.record_crawl = MagicMock(return_value=["alert"])
        hunter.schema_monitor.record_empty_crawl = MagicMock(return_value=[])
        bad, good = _Channel(fail=True), _Channel()
        hunter._alert_notifiers = [bad, good]
        hunter._record_health([])  # must not raise
        self.assertEqual(good.messages, ["alert"])


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
        return _build_hunter(self._tmpdir, target_urls, searchers)

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
        gewobag = _FakeSearcher("Gewobag", r"https://www\.gewobag\.de",
                                items=[{"crawler": "Gewobag", "title": "Y"}])
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
        return _build_hunter(self._tmpdir, target_urls, searchers)

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
        channel = _Channel()
        hunter._alert_notifiers = [channel]
        hunter._hunt_flats_inner = MagicMock(side_effect=RuntimeError("dead"))
        # Default threshold is 3 consecutive empties → 3 cycles trigger alert
        for _ in range(3):
            hunter.hunt_flats()
        self.assertEqual(len(channel.messages), 1)


class TestSiteReportedEmpty(unittest.TestCase):

    def test_site_reported_empty_is_healthy(self):
        tmp_obj = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_obj.cleanup)
        tmp = tmp_obj.name
        wbm = _FakeSearcher("Wbm", r"https://www\.wbm\.de")
        wbm.last_crawl_site_reported_empty = True
        gewobag = _FakeSearcher("Gewobag", r"https://www\.gewobag\.de")
        hunter = _build_hunter(tmp, ["https://www.wbm.de/x", "https://www.gewobag.de/y"],
                               [wbm, gewobag])
        hunter._record_health([])
        health = hunter.schema_monitor.get_health_summary()
        self.assertEqual(health["Wbm"]["consecutive_empty"], 0)
        self.assertEqual(health["Gewobag"]["consecutive_empty"], 1)


TELEGRAM_URL = "https://api.telegram.org/botTOK/sendMessage"


class TestPerListingNotifiers(unittest.TestCase):
    """`notifiers` decides whether listings are sent; alert channels don't."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _hunter(self, notifiers):
        hunter = _build_hunter(
            self._tmpdir, ["https://www.wbm.de/x"],
            [_FakeSearcher("Wbm", r"https://www\.wbm\.de")],
            notifiers=notifiers,
            telegram={"bot_token": "TOK", "receiver_ids": [42]},
            monitoring={"alert_via_notifiers": True, "alert_notifiers": ["telegram"]},
        )
        self.addCleanup(hunter.close)
        return hunter

    def _listing(self):
        return {"crawler": "Wbm", "id": 7, "title": "Wohnung", "url": "https://www.wbm.de/a",
                "address": "Straße 1", "price": "500 €", "description": ""}

    def test_empty_notifiers_sends_no_listing(self):
        hunter = self._hunter([])
        with req_mock.Mocker() as m:
            m.post(TELEGRAM_URL, json={"ok": True})
            result = hunter.process_raw([self._listing()])
        self.assertEqual(len(result), 1)  # still processed (marked seen, applied, ...)
        self.assertEqual(m.request_history, [])

    def test_empty_notifiers_keeps_alert_channel(self):
        hunter = self._hunter([])
        with req_mock.Mocker() as m:
            m.post(TELEGRAM_URL, json={"ok": True})
            hunter._dispatch_alerts(["[SCHEMA ALERT] Wbm down"])
        self.assertEqual(len(m.request_history), 1)
        self.assertIn("Wbm+down", m.request_history[0].text)

    def test_telegram_notifier_sends_listing(self):
        hunter = self._hunter(["telegram"])
        with req_mock.Mocker() as m:
            m.post(TELEGRAM_URL, json={"ok": True})
            hunter.process_raw([self._listing()])
        self.assertEqual(len(m.request_history), 1)
        self.assertIn("Wohnung", m.request_history[0].text)

    def test_profile_hunter_builds_no_schema_monitor(self):
        hunter = self._hunter([])
        hunter.process_raw([self._listing()])
        self.assertNotIn("schema_monitor", vars(hunter))
        self.assertFalse(os.path.exists(os.path.join(self._tmpdir, "schema_monitor.json")))


if __name__ == "__main__":
    unittest.main()
