"""Tests for AutoApplicator processor"""
import unittest
from unittest.mock import MagicMock, patch

from berlin_flat_hunter.applicator import (
    AutoApplicator,
    GewobagApplicator,
    KleinanzeigenApplicator,
    ManualApplyRequired,
    WbmApplicator,
    _fill_field,
)

GEWOBAG_EXPOSE = {
    "id": 1,
    "url": "https://www.gewobag.de/fuer-mietinteressentinnen/mietangebote/0100-01234/",
    "title": "Schöne 2-Zimmer-Wohnung",
    "address": "Beispielstraße 1, 10115 Berlin",
    "rooms": "2 Zi.",
    "size": "65.50 m²",
    "price": "900.00 €",
    "crawler": "Gewobag",
}

WBM_EXPOSE = {
    "id": 2,
    "url": "https://www.wbm.de/wohnungen-berlin/angebote/details/wohnung-mitte-abc/",
    "title": "3-Zimmer-Wohnung in Mitte",
    "address": "Alexanderplatz 5, 10178 Berlin",
    "rooms": "3",
    "size": "72,00 m²",
    "price": "850,00 €",
    "crawler": "Wbm",
}

KLEINANZEIGEN_EXPOSE = {
    "id": 3,
    "url": "https://www.kleinanzeigen.de/s-anzeige/wohnung/1234567890",
    "title": "Wohnung in Berlin",
    "crawler": "Kleinanzeigen",
}

APPLICANT = {
    "name": "Max Mustermann",
    "email": "max@example.com",
    "phone": "+49 30 1234567",
    "message": "Ich interessiere mich für diese Wohnung.",
}


class FakeConfig:
    def __init__(self, apply_cfg=None):
        self.config = {"auto_apply": apply_cfg or {}}

    def applicant_config(self):
        return APPLICANT


class TestFillField(unittest.TestCase):
    """_fill_field tries selectors in order, fills the first match."""

    def test_fills_first_matching_selector(self):
        driver = MagicMock()
        # first selector throws, second succeeds
        field = MagicMock()
        driver.find_element.side_effect = [Exception("not found"), field]
        result = _fill_field(driver, "input.a, input.b", "value")
        self.assertTrue(result)
        field.clear.assert_called_once()
        field.send_keys.assert_called_once_with("value")

    def test_returns_false_when_no_selector_matches(self):
        driver = MagicMock()
        driver.find_element.side_effect = Exception("not found")
        result = _fill_field(driver, "input.a, input.b", "value")
        self.assertFalse(result)

    def test_skips_when_value_empty(self):
        driver = MagicMock()
        result = _fill_field(driver, "input", "")
        self.assertFalse(result)
        driver.find_element.assert_not_called()

    def test_stops_after_first_success(self):
        driver = MagicMock()
        field = MagicMock()
        driver.find_element.return_value = field
        _fill_field(driver, "input.a, input.b", "value")
        # Should only try selector once (the first one matches)
        self.assertEqual(driver.find_element.call_count, 1)


class TestGewobagApplicator(unittest.TestCase):

    def setUp(self):
        self.app = GewobagApplicator(APPLICANT)

    def test_skips_non_gewobag_url(self):
        self.assertFalse(self.app.apply(WBM_EXPOSE))

    def test_url_match_field(self):
        self.assertEqual(self.app.URL_MATCH, "gewobag.de")

    def test_apply_returns_false_without_selenium(self):
        with patch.dict("sys.modules", {"selenium": None, "selenium.webdriver": None}):
            self.assertFalse(self.app.apply(GEWOBAG_EXPOSE))


class TestWbmApplicator(unittest.TestCase):

    def setUp(self):
        self.app = WbmApplicator(APPLICANT)

    def test_skips_non_wbm_url(self):
        self.assertFalse(self.app.apply(GEWOBAG_EXPOSE))

    def test_url_match_field(self):
        self.assertEqual(self.app.URL_MATCH, "wbm.de")

    def test_apply_returns_false_without_selenium(self):
        with patch.dict("sys.modules", {"selenium": None, "selenium.webdriver": None}):
            self.assertFalse(self.app.apply(WBM_EXPOSE))


class TestKleinanzeigenApplicator(unittest.TestCase):

    def test_skips_non_kleinanzeigen_url(self):
        app = KleinanzeigenApplicator(APPLICANT)
        self.assertFalse(app.apply(GEWOBAG_EXPOSE))

    def test_missing_credentials_raises_manual_apply(self):
        app = KleinanzeigenApplicator({"message": "hi"})
        with self.assertRaises(ManualApplyRequired):
            app.apply(KLEINANZEIGEN_EXPOSE)

    def test_only_email_raises_manual_apply(self):
        app = KleinanzeigenApplicator({"kleinanzeigen_email": "u@e.com"})
        with self.assertRaises(ManualApplyRequired):
            app.apply(KLEINANZEIGEN_EXPOSE)

    def test_only_password_raises_manual_apply(self):
        app = KleinanzeigenApplicator({"kleinanzeigen_password": "x"})
        with self.assertRaises(ManualApplyRequired):
            app.apply(KLEINANZEIGEN_EXPOSE)

    def test_falls_back_to_applicant_email(self):
        """When kleinanzeigen_email is not set, fall back to applicant.email."""
        app = KleinanzeigenApplicator({"email": "fallback@e.com", "kleinanzeigen_password": "x"})
        # applicant.email + password set → proceeds past credential check; selenium import fails inside (no chrome)
        with patch.dict("sys.modules", {"selenium": None, "selenium.webdriver": None}):
            self.assertFalse(app.apply(KLEINANZEIGEN_EXPOSE))
        # But should NOT have logged the "not configured" warning since email IS available via fallback.
        # (Negative assertion — the function must have proceeded past the check.)


class TestAutoApplicator(unittest.TestCase):

    def setUp(self):
        self.processor = AutoApplicator(FakeConfig())

    def test_has_three_applicators(self):
        self.assertEqual(len(self.processor.applicators), 3)

    def test_applicator_order(self):
        self.assertIsInstance(self.processor.applicators[0], GewobagApplicator)
        self.assertIsInstance(self.processor.applicators[1], WbmApplicator)
        self.assertIsInstance(self.processor.applicators[2], KleinanzeigenApplicator)

    def test_process_expose_returns_dict(self):
        for app in self.processor.applicators:
            app.apply = MagicMock(return_value=False)
        result = self.processor.process_expose(dict(GEWOBAG_EXPOSE))
        self.assertIsInstance(result, dict)

    def test_applied_flag_set_on_success(self):
        self.processor.applicators[0].apply = MagicMock(return_value=True)
        result = self.processor.process_expose(dict(GEWOBAG_EXPOSE))
        self.assertTrue(result.get("applied"))

    def test_no_applied_flag_when_all_fail(self):
        for app in self.processor.applicators:
            app.apply = MagicMock(return_value=False)
        result = self.processor.process_expose(dict(WBM_EXPOSE))
        self.assertNotIn("applied", result)

    def test_first_match_stops_further_applicators(self):
        m0 = self.processor.applicators[0].apply = MagicMock(return_value=True)
        m1 = self.processor.applicators[1].apply = MagicMock(return_value=False)
        m2 = self.processor.applicators[2].apply = MagicMock(return_value=False)
        self.processor.process_expose(dict(GEWOBAG_EXPOSE))
        m0.assert_called_once()
        m1.assert_not_called()
        m2.assert_not_called()

    def test_later_applicator_tried_after_earlier_fails(self):
        m0 = self.processor.applicators[0].apply = MagicMock(return_value=False)
        m1 = self.processor.applicators[1].apply = MagicMock(return_value=True)
        self.processor.applicators[2].apply = MagicMock(return_value=False)
        self.processor.process_expose(dict(WBM_EXPOSE))
        m0.assert_called_once()
        m1.assert_called_once()


class TestStaleApplicatorAlerts(unittest.TestCase):
    """Failure counter + alert dispatch when an applicator's URL matches
    but apply() keeps returning False (selectors stale)."""

    def setUp(self):
        self.dispatched: list[list[str]] = []
        self.processor = AutoApplicator(
            FakeConfig(), alert_dispatch=lambda msgs: self.dispatched.append(msgs),
        )
        # Fail every applicator so URL_MATCH counts but no success masks it.
        for app in self.processor.applicators:
            app.apply = MagicMock(return_value=False)

    def test_alert_after_threshold_consecutive_failures(self):
        for _ in range(3):
            self.processor.process_expose(dict(GEWOBAG_EXPOSE))
        self.assertEqual(len(self.dispatched), 1)
        msg = self.dispatched[0][0]
        self.assertIn("Gewobag", msg)
        self.assertIn("APPLICATOR ALERT", msg)

    def test_no_alert_below_threshold(self):
        for _ in range(2):
            self.processor.process_expose(dict(GEWOBAG_EXPOSE))
        self.assertEqual(self.dispatched, [])

    def test_success_resets_counter(self):
        for _ in range(2):
            self.processor.process_expose(dict(GEWOBAG_EXPOSE))
        # Now succeed
        self.processor.applicators[0].apply = MagicMock(return_value=True)
        self.processor.process_expose(dict(GEWOBAG_EXPOSE))
        # Back to failing — counter starts from 0
        self.processor.applicators[0].apply = MagicMock(return_value=False)
        for _ in range(2):
            self.processor.process_expose(dict(GEWOBAG_EXPOSE))
        # Total: 2 fail + 1 success + 2 fail. Below 3 since reset → no alert.
        self.assertEqual(self.dispatched, [])

    def test_failures_on_non_matching_urls_dont_count(self):
        """A WBM URL doesn't trigger the Gewobag failure counter even though
        Gewobag.apply() is invoked first in the loop — its URL_MATCH guard fails."""
        # Gewobag applicator returns False because URL_MATCH=gewobag.de is
        # not in the WBM URL — but is_target=False so the counter must NOT tick.
        for _ in range(5):
            self.processor.process_expose(dict(WBM_EXPOSE))
        # WBM applicator URL_MATCHes — its 5 failures should hit threshold once.
        self.assertEqual(len(self.dispatched), 1)
        self.assertIn("WBM", self.dispatched[0][0])

    def test_cooldown_blocks_repeat_alert(self):
        for _ in range(6):
            self.processor.process_expose(dict(GEWOBAG_EXPOSE))
        # 6 failures, but cooldown is 1h — only one alert dispatched.
        self.assertEqual(len(self.dispatched), 1)


class TestManualApplyNotifications(unittest.TestCase):
    """ManualApplyRequired raises become per-listing notifier dispatches —
    one per listing, no cooldown, doesn't tick the stale-selector counter."""

    def setUp(self):
        self.dispatched: list[list[str]] = []
        self.processor = AutoApplicator(
            FakeConfig(), alert_dispatch=lambda msgs: self.dispatched.append(msgs),
        )

    def test_recaptcha_dispatches_manual_apply_alert(self):
        self.processor.applicators[0].apply = MagicMock(
            side_effect=ManualApplyRequired("reCAPTCHA challenge"))
        result = self.processor.process_expose(dict(GEWOBAG_EXPOSE))
        self.assertTrue(result.get("manual_apply_required"))
        self.assertEqual(len(self.dispatched), 1)
        msg = self.dispatched[0][0]
        self.assertIn("MANUAL APPLY", msg)
        self.assertIn("Gewobag", msg)
        self.assertIn("reCAPTCHA", msg)
        self.assertIn(GEWOBAG_EXPOSE["url"], msg)

    def test_manual_apply_does_not_tick_stale_counter(self):
        """A site demanding humans (reCAPTCHA) is not the same as broken
        selectors — must not eventually fire the [APPLICATOR ALERT]."""
        self.processor.applicators[0].apply = MagicMock(
            side_effect=ManualApplyRequired("reCAPTCHA challenge"))
        for _ in range(5):
            self.processor.process_expose(dict(GEWOBAG_EXPOSE))
        # 5 manual-apply alerts (one per listing) but no stale-selector alert.
        self.assertEqual(len(self.dispatched), 5)
        for batch in self.dispatched:
            self.assertIn("MANUAL APPLY", batch[0])
            self.assertNotIn("APPLICATOR ALERT", batch[0])

    def test_manual_apply_per_listing_no_cooldown(self):
        """Manual-apply alerts are per-listing actionable (apply by hand on
        this URL) — must NOT be deduplicated by a 1h cooldown like stale alerts."""
        self.processor.applicators[0].apply = MagicMock(
            side_effect=ManualApplyRequired("reCAPTCHA challenge"))
        for _ in range(3):
            self.processor.process_expose(dict(GEWOBAG_EXPOSE))
        self.assertEqual(len(self.dispatched), 3)

    def test_manual_apply_skips_remaining_applicators(self):
        m0 = self.processor.applicators[0].apply = MagicMock(
            side_effect=ManualApplyRequired("reCAPTCHA"))
        m1 = self.processor.applicators[1].apply = MagicMock(return_value=True)
        self.processor.process_expose(dict(GEWOBAG_EXPOSE))
        m0.assert_called_once()
        m1.assert_not_called()

    def test_no_alert_dispatch_callback_still_logs(self):
        """When alert_dispatch is None the message should still be logged at
        INFO level — the absence of telegram doesn't mean the user gets nothing."""
        proc = AutoApplicator(FakeConfig(), alert_dispatch=None)
        proc.applicators[0].apply = MagicMock(
            side_effect=ManualApplyRequired("reCAPTCHA"))
        with self.assertLogs("flathunt", level="INFO") as cm:
            proc.process_expose(dict(GEWOBAG_EXPOSE))
        self.assertTrue(any("MANUAL APPLY" in line for line in cm.output))


if __name__ == "__main__":
    unittest.main()
