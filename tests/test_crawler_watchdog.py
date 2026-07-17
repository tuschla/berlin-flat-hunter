"""Tests for the crawler watchdog's pure decision logic (no Claude/Telegram)."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import crawler_watchdog as wd  # noqa: E402


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
        self.assertIn("Dezsö", wd.DEZSO_NOTE)
        self.assertIn("Leon", wd.DEZSO_NOTE)

    def test_prompt_asks_for_italian_output(self):
        prompt = wd.build_prompt("wg", "Wbm",
                                 {"last_success_ts": 0, "consecutive_empty": 5}, [])
        self.assertIn("ITALIAN", prompt)


if __name__ == "__main__":
    unittest.main()
