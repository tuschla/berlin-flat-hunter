"""Tests for ScamFilter — keyword denylist for kleinanzeigen exposes"""
import unittest

from berlin_flat_hunter.filters.scam_filter import DEFAULT_PATTERNS, ScamFilter


class FakeConfig:
    def __init__(self, cfg=None):
        self._cfg = cfg or {"enabled": True}

    def scam_filter_config(self):
        return self._cfg


def _ka(title="Wohnung in Berlin", address="Beispielstraße 1, 10115 Berlin",
        crawler="Kleinanzeigen",
        url="https://www.kleinanzeigen.de/s-anzeige/wohnung/1234567890") -> dict:
    return {"id": 1, "title": title, "address": address, "crawler": crawler,
            "url": url, "price": "500 €"}


class TestScamFilter(unittest.TestCase):

    def setUp(self):
        self.f = ScamFilter(FakeConfig())

    def test_passes_clean_listing(self):
        out = list(self.f.process_exposes([_ka()]))
        self.assertEqual(len(out), 1)

    def test_drops_auslandsumzug(self):
        out = list(self.f.process_exposes([
            _ka(title="2-Zimmer wegen Auslandsumzug abzugeben"),
        ]))
        self.assertEqual(out, [])

    def test_drops_whatsapp_in_title(self):
        out = list(self.f.process_exposes([_ka(title="Schöne Wohnung Whatsapp +44…")]))
        self.assertEqual(out, [])

    def test_drops_email_in_title(self):
        out = list(self.f.process_exposes([_ka(title="Kontakt: scammer@example.com")]))
        self.assertEqual(out, [])

    def test_email_flag_disabled_keeps_listing(self):
        f = ScamFilter(FakeConfig({"enabled": True, "flag_email_in_title": False}))
        out = list(f.process_exposes([_ka(title="Kontakt: scammer@example.com")]))
        self.assertEqual(len(out), 1)

    def test_drops_western_union(self):
        out = list(self.f.process_exposes([
            _ka(title="Wohnung", address="Kaution Western Union"),
        ]))
        self.assertEqual(out, [])

    def test_case_insensitive(self):
        out = list(self.f.process_exposes([_ka(title="WOHNE IM AUSLAND")]))
        self.assertEqual(out, [])

    def test_passes_non_kleinanzeigen_with_scam_keywords(self):
        # Howoge/WBM titles never trigger this filter — they are pre-vetted
        # public landlords; if they ever say "WhatsApp" it's not a scam.
        out = list(self.f.process_exposes([_ka(
            title="WhatsApp uns für Termine",
            crawler="Howoge",
            url="https://www.howoge.de/abc",
        )]))
        self.assertEqual(len(out), 1)

    def test_url_host_fallback_when_crawler_field_missing(self):
        # If crawler tag is absent (some upstream paths drop it), still match
        # by URL host so kleinanzeigen scams can't slip through.
        out = list(self.f.process_exposes([{
            "title": "Auslandsumzug",
            "address": "",
            "url": "https://www.kleinanzeigen.de/x",
        }]))
        self.assertEqual(out, [])

    def test_extra_patterns_extend_defaults(self):
        f = ScamFilter(FakeConfig({
            "enabled": True,
            "extra_patterns": ["nigerian prince"],
        }))
        out = list(f.process_exposes([_ka(title="Nigerian prince offers flat")]))
        self.assertEqual(out, [])
        # Defaults still active.
        out = list(f.process_exposes([_ka(title="Auslandsumzug")]))
        self.assertEqual(out, [])

    def test_override_patterns_replaces_defaults(self):
        f = ScamFilter(FakeConfig({
            "enabled": True,
            "override_patterns": ["only-this-one"],
            "flag_email_in_title": False,
        }))
        # Default pattern no longer active.
        out = list(f.process_exposes([_ka(title="Auslandsumzug")]))
        self.assertEqual(len(out), 1)
        # Override pattern active.
        out = list(f.process_exposes([_ka(title="Hi only-this-one")]))
        self.assertEqual(out, [])

    def test_default_patterns_nonempty(self):
        # Guard against the curated list being accidentally cleared.
        self.assertGreater(len(DEFAULT_PATTERNS), 5)

    def test_empty_title_and_address_passes(self):
        out = list(self.f.process_exposes([_ka(title="", address="")]))
        self.assertEqual(len(out), 1)

    def test_pattern_logged_on_drop(self):
        with self.assertLogs("flathunt", level="INFO") as cm:
            list(self.f.process_exposes([_ka(title="Auslandsumzug ahoy")]))
        self.assertTrue(any("auslandsumzug" in line.lower() for line in cm.output))

    def test_drops_pattern_in_description(self):
        """Description body is where real KA scams put their tells — title is boring."""
        expose = _ka(title="2-Zimmer-Wohnung Neukölln", address="Karl-Marx-Str.")
        expose["description"] = ("Hello, I am currently abroad but you can contact me "
                                 "at owner@example.com via WhatsApp for viewing.")
        out = list(self.f.process_exposes([expose]))
        self.assertEqual(out, [])

    def test_drops_email_in_description(self):
        expose = _ka(title="2-Zimmer Neukölln", address="Beispielstraße 1")
        expose["description"] = "Bitte melden Sie sich per E-Mail: scammer@example.com"
        out = list(self.f.process_exposes([expose]))
        self.assertEqual(out, [])

    def test_email_in_description_flag_respects_disabled(self):
        f = ScamFilter(FakeConfig({"enabled": True, "flag_email_in_title": False}))
        expose = _ka()
        expose["description"] = "Kontakt: real@landlord.de"
        out = list(f.process_exposes([expose]))
        self.assertEqual(len(out), 1)

    def test_missing_description_still_scans_title_address(self):
        # Backward compat: exposes without description still get filtered on
        # title+address patterns.
        out = list(self.f.process_exposes([_ka(title="Wegen Auslandsumzug")]))
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
