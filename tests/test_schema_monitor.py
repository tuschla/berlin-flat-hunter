"""Tests for SchemaMonitor"""
import json
import os
import tempfile
import time
import unittest

from berlin_flat_hunter.monitoring.schema_monitor import SchemaMonitor, CRITICAL_FIELDS

EXPOSE_OK = {
    "id": 1, "crawler": "Gewobag",
    "url": "https://www.gewobag.de/1/", "title": "2-Zimmer-Wohnung",
    "address": "Musterstraße 1, 10115 Berlin", "price": "900 €",
}
EXPOSE_MISSING = {
    "id": 2, "crawler": "Gewobag",
    "url": "https://www.gewobag.de/2/", "title": "",
    "address": "", "price": "",
}
EXPOSE_WBM = {
    "id": 3, "crawler": "Wbm",
    "url": "https://www.wbm.de/1/", "title": "3-Zimmer",
    "address": "Alexanderplatz 1, 10178 Berlin", "price": "1100 €",
}


def _monitor(cfg=None, **kwargs):
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    os.unlink(path)  # start fresh
    return SchemaMonitor(path, cfg or {}), path


class TestSchemaMonitor(unittest.TestCase):

    def setUp(self):
        self._cleanup_paths = []

    def tearDown(self):
        for p in self._cleanup_paths:
            for ext in ("", ".tmp"):
                if os.path.exists(p + ext):
                    os.unlink(p + ext)

    def _new_monitor(self, cfg=None):
        mon, path = _monitor(cfg)
        self._cleanup_paths.append(path)
        return mon, path

    def test_no_alerts_on_healthy_crawl(self):
        mon, path = _monitor()
        alerts = mon.record_crawl([EXPOSE_OK])
        self.assertEqual(alerts, [])
        os.unlink(path)

    def test_no_alert_below_empty_threshold(self):
        mon, path = _monitor({"monitoring": {"consecutive_empty_threshold": 3}})
        mon.record_crawl([])
        mon.record_crawl([])
        alerts = mon.record_crawl([EXPOSE_OK])  # resets counter
        self.assertEqual(alerts, [])
        os.unlink(path)

    def test_alert_after_threshold_empty_runs(self):
        mon, path = _monitor({"monitoring": {"consecutive_empty_threshold": 2}})
        mon.record_empty_crawl("Gewobag")           # consecutive=1, below threshold
        alerts = mon.record_empty_crawl("Gewobag")  # consecutive=2, fires alert
        self.assertTrue(len(alerts) > 0)
        self.assertIn("Gewobag", alerts[0])
        os.unlink(path)

    def test_alert_on_high_field_miss_rate(self):
        mon, path = _monitor({"monitoring": {"field_miss_threshold": 0.5}})
        alerts = mon.record_crawl([EXPOSE_MISSING, EXPOSE_MISSING, EXPOSE_OK])
        self.assertTrue(len(alerts) > 0)
        self.assertIn("SCHEMA ALERT", alerts[0])
        os.unlink(path)

    def test_no_alert_below_field_miss_threshold(self):
        mon, path = _monitor({"monitoring": {"field_miss_threshold": 0.8}})
        # Only 1/3 missing — below 80% threshold
        alerts = mon.record_crawl([EXPOSE_MISSING, EXPOSE_OK, EXPOSE_OK])
        self.assertEqual(alerts, [])
        os.unlink(path)

    def test_empty_run_increments_counter(self):
        mon, path = _monitor()
        mon.record_empty_crawl("Gewobag")
        mon.record_empty_crawl("Gewobag")
        health = mon.get_health_summary()
        self.assertEqual(health["Gewobag"]["consecutive_empty"], 2)
        os.unlink(path)

    def test_successful_crawl_resets_counter(self):
        mon, path = _monitor()
        mon.record_empty_crawl("Gewobag")
        mon.record_empty_crawl("Gewobag")
        mon.record_crawl([EXPOSE_OK])
        health = mon.get_health_summary()
        self.assertEqual(health["Gewobag"]["consecutive_empty"], 0)
        os.unlink(path)

    def test_last_success_ts_updated_on_results(self):
        mon, path = _monitor()
        before = time.time()
        mon.record_crawl([EXPOSE_OK])
        health = mon.get_health_summary()
        self.assertGreaterEqual(health["Gewobag"]["last_success_ts"], before)
        os.unlink(path)

    def test_state_persists_across_instances(self):
        mon, path = _monitor()
        mon.record_empty_crawl("Gewobag")
        mon.record_empty_crawl("Gewobag")
        # New instance loads same state file
        mon2 = SchemaMonitor(path, {})
        health = mon2.get_health_summary()
        self.assertEqual(health["Gewobag"]["consecutive_empty"], 2)
        os.unlink(path)

    def test_state_saved_to_json(self):
        mon, path = _monitor()
        mon.record_empty_crawl("Gewobag")
        with open(path) as f:
            data = json.load(f)
        self.assertIn("Gewobag", data)
        self.assertEqual(data["Gewobag"]["consecutive_empty"], 1)
        os.unlink(path)

    def test_multiple_crawlers_tracked_separately(self):
        mon, path = _monitor()
        mon.record_crawl([EXPOSE_OK, EXPOSE_WBM])
        mon.record_empty_crawl("Gewobag")
        health = mon.get_health_summary()
        self.assertEqual(health["Gewobag"]["consecutive_empty"], 1)
        self.assertEqual(health["Wbm"]["consecutive_empty"], 0)
        os.unlink(path)

    def test_alert_cooldown_suppresses_repeated_alerts(self):
        mon, path = _monitor({"monitoring": {"consecutive_empty_threshold": 1}})
        alerts1 = mon.record_empty_crawl("Gewobag")  # consecutive=1, fires
        alerts2 = mon.record_empty_crawl("Gewobag")  # in cooldown
        alerts3 = mon.record_empty_crawl("Gewobag")  # in cooldown
        self.assertTrue(len(alerts1) > 0)     # first alert fires
        self.assertEqual(len(alerts2), 0)     # suppressed by cooldown
        self.assertEqual(len(alerts3), 0)
        os.unlink(path)

    def test_missing_state_file_handled_gracefully(self):
        path = tempfile.mktemp(suffix="_monitor.json")
        self._cleanup_paths.append(path)
        mon = SchemaMonitor(path, {})
        alerts = mon.record_crawl([EXPOSE_OK])
        self.assertEqual(alerts, [])

    def test_default_thresholds(self):
        mon, path = _monitor()
        self.assertEqual(mon.empty_threshold, 3)
        self.assertAlmostEqual(mon.field_miss_threshold, 0.5)
        if os.path.exists(path):
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
