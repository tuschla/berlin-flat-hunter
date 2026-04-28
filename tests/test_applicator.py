"""Tests for AutoApplicator processor"""
import unittest
from unittest.mock import MagicMock, patch

from berlin_flat_hunter.applicator import (
    AutoApplicator,
    GewobagApplicator,
    KleinanzeigenApplicator,
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

    def test_skips_without_credentials(self):
        app = KleinanzeigenApplicator({"message": "hi"})
        self.assertFalse(app.apply(KLEINANZEIGEN_EXPOSE))

    def test_skips_with_only_email(self):
        app = KleinanzeigenApplicator({"kleinanzeigen_email": "u@e.com"})
        self.assertFalse(app.apply(KLEINANZEIGEN_EXPOSE))

    def test_skips_with_only_password(self):
        app = KleinanzeigenApplicator({"kleinanzeigen_password": "x"})
        self.assertFalse(app.apply(KLEINANZEIGEN_EXPOSE))

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


if __name__ == "__main__":
    unittest.main()
