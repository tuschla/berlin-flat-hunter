"""Tests for the crawler watchdog's decision logic and alert senders."""
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


class TestShouldAlert(unittest.TestCase):

    def setUp(self):
        self.health = {"last_success_ts": 1000.0, "consecutive_alerts": 1}

    def test_new_episode_alerts(self):
        # Never handled this crawler before.
        self.assertTrue(wd.should_alert({}, "shared:Wbm", self.health, False))

    def test_same_episode_alerts_once(self):
        state = {"shared:Wbm": {"handled_success_ts": 1000.0, "alert_ts": time.time()}}
        self.assertFalse(wd.should_alert(state, "shared:Wbm", self.health, False))

    def test_recovered_then_down_again_is_new_episode(self):
        # We handled the previous episode (last_success 1000); the crawler has
        # since succeeded (last_success now 2000) and broken again → alert again.
        state = {"shared:Wbm": {"handled_success_ts": 1000.0, "alert_ts": time.time()}}
        fresh = {"last_success_ts": 2000.0, "consecutive_alerts": 1}
        self.assertTrue(wd.should_alert(state, "shared:Wbm", fresh, False))

    def test_force_always_alerts(self):
        state = {"shared:Wbm": {"handled_success_ts": 1000.0, "alert_ts": time.time()}}
        self.assertTrue(wd.should_alert(state, "shared:Wbm", self.health, True))


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
        self.ntfy: list[tuple[str, str]] = []
        for patcher in (
            mock.patch.object(wd, "HUNTER_YAML", hunter_yaml),
            mock.patch.object(wd, "WATCHDOG_STATE", os.path.join(self.tmp.name, "wd.json")),
            mock.patch.object(wd, "send_telegram",
                              lambda token, ids, text: self.sent.append(text) or True),
            mock.patch.object(wd, "send_ntfy",
                              lambda title, text: self.ntfy.append((title, text)) or True),
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
        wd.main()  # next cron tick, still stuck on the same cycle
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Restart", self.sent[0])
        self.assertIn(wd.DEZSO_NOTE, self.sent[0])
        # Same alert text goes to ntfy, without the note for Dezső.
        self.assertEqual(len(self.ntfy), 1)
        title, text = self.ntfy[0]
        self.assertIn("stalled", title)
        self.assertTrue(self.sent[0].startswith(text))
        self.assertNotIn(wd.DEZSO_NOTE, text)

    def test_down_crawler_alerts_once(self):
        with open(self.monitor, "w") as f:
            json.dump({"Wbm": {"consecutive_empty": 5, "consecutive_alerts": 1,
                               "last_success_ts": 1000.0}}, f)
        wd.main()
        wd.main()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Wbm", self.sent[0])
        self.assertEqual(self.ntfy[0][0], "flathunter: crawler Wbm down")

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

    def test_send_telegram_without_creds_returns_false(self):
        self.assertFalse(wd.send_telegram("", [], "hi"))
        self.assertFalse(wd.send_telegram("token", [], "hi"))

    def test_dezso_note_addresses_dezso_and_leon(self):
        self.assertIn("Dezső", wd.DEZSO_NOTE)
        self.assertIn("Leon", wd.DEZSO_NOTE)


class TestSendNtfy(unittest.TestCase):

    def test_noop_without_ntfy_url(self):
        with mock.patch.object(wd, "NTFY_URL", ""), \
                mock.patch.object(wd.urllib.request, "urlopen") as urlopen:
            self.assertFalse(wd.send_ntfy("t", "body"))
        urlopen.assert_not_called()

    def test_posts_title_and_body(self):
        resp = mock.MagicMock(status=200)
        resp.__enter__.return_value = resp
        with mock.patch.object(wd, "NTFY_URL", "https://ntfy.example/topic"), \
                mock.patch.object(wd.urllib.request, "urlopen", return_value=resp) as urlopen:
            self.assertTrue(wd.send_ntfy("flathunter: crawler Wbm down", "il crawler Wbm…"))
        req = urlopen.call_args.args[0]
        self.assertEqual(req.full_url, "https://ntfy.example/topic")
        self.assertEqual(req.get_header("Title"), "flathunter: crawler Wbm down")
        self.assertEqual(req.get_header("Tags"), "warning")
        self.assertEqual(req.data, "il crawler Wbm…".encode())

    def test_unreachable_ntfy_does_not_raise(self):
        with mock.patch.object(wd, "NTFY_URL", "https://ntfy.example/topic"), \
                mock.patch.object(wd.urllib.request, "urlopen",
                                  side_effect=wd.urllib.error.URLError("down")):
            self.assertFalse(wd.send_ntfy("t", "body"))


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
