"""Tests for the crawler watchdog's pure decision logic (no Claude/Telegram)."""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

import requests_mock as req_mock  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import crawler_watchdog as wd  # noqa: E402
import probe_url  # noqa: E402


class TestDownDetection(unittest.TestCase):

    def test_flags_crawler_with_active_alert(self):
        state = {
            "Kleinanzeigen": {"consecutive_empty": 14, "consecutive_alerts": 1},
            "Gewobag": {"consecutive_empty": 0, "consecutive_alerts": 0},
        }
        down = dict(wd.down_crawlers(state))
        self.assertIn("Kleinanzeigen", down)
        self.assertNotIn("Gewobag", down)

    def test_empty_but_below_alert_threshold_not_down(self):
        # consecutive_empty ticking up but no alert yet → not yet flagged.
        state = {"Wbm": {"consecutive_empty": 2, "consecutive_alerts": 0}}
        self.assertEqual(wd.down_crawlers(state), [])

    def test_ignores_non_dict_entries(self):
        self.assertEqual(wd.down_crawlers({"junk": "x", "n": 1}), [])

    def test_empty_state(self):
        self.assertEqual(wd.down_crawlers({}), [])


class TestShouldTrigger(unittest.TestCase):

    def setUp(self):
        self.now = time.time()
        self.health = {"last_success_ts": 1000.0, "consecutive_alerts": 1}

    def test_new_episode_triggers(self):
        # Never handled this crawler before.
        self.assertTrue(wd.should_trigger({}, "single:KA", self.health, self.now, False))

    def test_same_episode_within_cooldown_skips(self):
        state = {"single:KA": {"handled_success_ts": 1000.0, "trigger_ts": self.now - 60}}
        self.assertFalse(wd.should_trigger(state, "single:KA", self.health, self.now, False))

    def test_same_episode_past_cooldown_retriggers(self):
        old = self.now - (wd.RETRIGGER_COOLDOWN + 10)
        state = {"single:KA": {"handled_success_ts": 1000.0, "trigger_ts": old}}
        self.assertTrue(wd.should_trigger(state, "single:KA", self.health, self.now, False))

    def test_recovered_then_down_again_is_new_episode(self):
        # We handled the previous episode (last_success 1000); the crawler has
        # since succeeded (last_success now 2000) and broken again → re-triage.
        state = {"single:KA": {"handled_success_ts": 1000.0, "trigger_ts": self.now - 60}}
        fresh = {"last_success_ts": 2000.0, "consecutive_alerts": 1}
        self.assertTrue(wd.should_trigger(state, "single:KA", fresh, self.now, False))

    def test_force_always_triggers(self):
        state = {"single:KA": {"handled_success_ts": 1000.0, "trigger_ts": self.now}}
        self.assertTrue(wd.should_trigger(state, "single:KA", self.health, self.now, True))


class TestStallAlert(unittest.TestCase):
    """A hunter stuck mid-cycle stops rewriting schema_monitor.json; per-crawler
    health is frozen then, so the watchdog must alert on the file's age."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        hunter_yaml = os.path.join(self.tmp.name, "hunter.yaml")
        with open(hunter_yaml, "w") as f:
            f.write(f"global:\n  database_location: {self.tmp.name}/db.sqlite\n"
                    "  telegram:\n    bot_token: tok\n    receiver_ids: [42]\n")
        self.monitor = os.path.join(self.tmp.name, "schema_monitor.json")
        with open(self.monitor, "w") as f:
            json.dump({"Howoge": {"consecutive_empty": 0, "consecutive_alerts": 0}}, f)
        self.sent: list[str] = []
        for patcher in (
            mock.patch.object(wd, "HUNTER_YAML", hunter_yaml),
            mock.patch.object(wd, "WATCHDOG_STATE", os.path.join(self.tmp.name, "wd.json")),
            mock.patch.object(wd, "send_telegram",
                              lambda token, ids, text: self.sent.append(text) or True),
            mock.patch.object(sys, "argv", ["crawler_watchdog.py"]),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _last_cycle_ago(self, seconds: float):
        ts = time.time() - seconds
        os.utime(self.monitor, (ts, ts))

    def test_recent_cycle_does_not_alert(self):
        self._last_cycle_ago(wd.STALL_AFTER - 60)
        wd.main()
        self.assertEqual(self.sent, [])

    def test_stalled_cycle_alerts_once_per_episode(self):
        self._last_cycle_ago(wd.STALL_AFTER + 60)
        wd.main()
        wd.main()  # next timer tick, still stuck on the same cycle
        self.assertEqual(len(self.sent), 1)
        self.assertIn("bfh-hunter", self.sent[0])

    def test_new_stall_after_recovery_alerts_again(self):
        self._last_cycle_ago(wd.STALL_AFTER + 600)
        wd.main()
        self._last_cycle_ago(wd.STALL_AFTER + 60)  # cycles resumed, then stuck again
        wd.main()
        self.assertEqual(len(self.sent), 2)


class TestHelpers(unittest.TestCase):

    def test_human_ago_never(self):
        self.assertEqual(wd.human_ago(0), ("never", "?"))

    def test_human_ago_formats_recent(self):
        when, ago = wd.human_ago(time.time() - 3720)  # 1h 2m
        self.assertIn("UTC", when)
        self.assertTrue(ago.endswith("m"))
        self.assertIn("1h", ago)

    def test_build_prompt_contains_key_facts(self):
        prompt = wd.build_prompt(
            "single", "Kleinanzeigen",
            {"last_success_ts": time.time() - 600, "consecutive_empty": 12},
            ["https://www.kleinanzeigen.de/s-wohnung-mieten/berlin/"],
        )
        self.assertIn("Kleinanzeigen", prompt)
        self.assertIn("READ-ONLY", prompt)
        self.assertIn("kleinanzeigen.py", prompt)
        self.assertIn("kleinanzeigen.de/s-wohnung-mieten", prompt)

    def test_send_telegram_without_creds_returns_false(self):
        self.assertFalse(wd.send_telegram("", [], "hi"))
        self.assertFalse(wd.send_telegram("token", [], "hi"))

    def test_dezso_note_addresses_dezso_and_leon(self):
        self.assertIn("Dezső", wd.DEZSO_NOTE)
        self.assertIn("Leon", wd.DEZSO_NOTE)

    def test_prompt_asks_for_italian_output(self):
        prompt = wd.build_prompt("wg", "Wbm",
                                 {"last_success_ts": 0, "consecutive_empty": 5}, [])
        self.assertIn("ITALIAN", prompt)

    def test_prompt_tells_triage_to_use_box_side_probe(self):
        prompt = wd.build_prompt("single", "Kleinanzeigen",
                                 {"last_success_ts": 0, "consecutive_empty": 9}, [])
        self.assertIn("probe_url.py", prompt)
        self.assertIn("WebFetch", prompt)  # warns about the egress trap

    def test_probe_is_allowlisted_for_triage(self):
        self.assertIn("Bash(.venv/bin/python scripts/probe_url.py:*)",
                      wd.CLAUDE_ALLOWED_TOOLS)


class TestProbeUrl(unittest.TestCase):

    def test_reports_status_and_listing_marker(self):
        url = "https://www.kleinanzeigen.de/x"
        html = '<html><body><div id="srchrslt-adtable">ok</div></body></html>'
        with req_mock.Mocker() as m:
            m.get(url, text=html, status_code=200)
            out = probe_url.probe(url)
        self.assertIn("HTTP 200", out)
        self.assertIn("srchrslt-adtable", out)

    def test_reports_captcha_marker(self):
        url = "https://www.kleinanzeigen.de/y"
        with req_mock.Mocker() as m:
            m.get(url, text="please solve the captcha", status_code=200)
            out = probe_url.probe(url)
        self.assertIn("captcha", out)

    def test_no_markers_note(self):
        url = "https://example.com/z"
        with req_mock.Mocker() as m:
            m.get(url, text="<html>nothing here</html>", status_code=200)
            out = probe_url.probe(url)
        self.assertIn("no known", out)

    def test_fetch_error_is_reported_not_raised(self):
        url = "https://example.com/err"
        with req_mock.Mocker() as m:
            import requests
            m.get(url, exc=requests.exceptions.ConnectTimeout)
            out = probe_url.probe(url)
        self.assertTrue(out.startswith("FETCH_ERROR"))


if __name__ == "__main__":
    unittest.main()
