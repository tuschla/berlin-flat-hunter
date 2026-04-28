"""Tests for Gewobag crawler"""
import json
import os
import unittest

import requests
import requests_mock as req_mock

from berlin_flat_hunter.crawlers.gewobag import Gewobag, WP_API_URL

FIXTURE_INDEX = os.path.join(os.path.dirname(__file__), "fixtures", "gewobag_index.json")
FIXTURE_DETAIL = os.path.join(os.path.dirname(__file__), "fixtures", "gewobag_detail.html")
SEARCH_URL = "https://www.gewobag.de/fuer-mietinteressentinnen/mietangebote/"


class FakeConfig:
    def captcha_enabled(self):
        return False
    def use_proxy(self):
        return False


def _index():
    with open(FIXTURE_INDEX) as f:
        return json.load(f)


def _detail_html():
    with open(FIXTURE_DETAIL) as f:
        return f.read()


def _register_mocks(m, index=None, detail_status=200):
    """Register API index + detail page mocks."""
    items = index if index is not None else _index()
    # First page returns items, second returns empty (stop pagination)
    m.register_uri(
        req_mock.GET, WP_API_URL,
        [
            {"json": items, "status_code": 200},
            {"json": [], "status_code": 200},
        ],
    )
    # Detail pages
    for item in items:
        m.register_uri(
            req_mock.GET, item["link"],
            text=_detail_html(), status_code=detail_status,
        )


class TestGewobagCrawler(unittest.TestCase):

    def setUp(self):
        self.crawler = Gewobag(FakeConfig())

    def test_get_results_returns_list(self):
        with req_mock.Mocker() as m:
            _register_mocks(m)
            results = self.crawler.get_results(SEARCH_URL)
        self.assertIsInstance(results, list)

    def test_stellplatz_excluded(self):
        with req_mock.Mocker() as m:
            _register_mocks(m)
            results = self.crawler.get_results(SEARCH_URL)
        urls = [r["url"] for r in results]
        self.assertFalse(any("5051" in u for u in urls))

    def test_apartments_included(self):
        with req_mock.Mocker() as m:
            _register_mocks(m)
            results = self.crawler.get_results(SEARCH_URL)
        self.assertGreaterEqual(len(results), 1)

    def test_entry_has_required_keys(self):
        with req_mock.Mocker() as m:
            _register_mocks(m)
            results = self.crawler.get_results(SEARCH_URL)
        for key in ("id", "url", "title", "address", "rooms", "size", "price", "crawler"):
            self.assertIn(key, results[0])

    def test_id_is_int_from_api(self):
        with req_mock.Mocker() as m:
            _register_mocks(m)
            results = self.crawler.get_results(SEARCH_URL)
        self.assertIsInstance(results[0]["id"], int)

    def test_address_parsed_from_detail(self):
        with req_mock.Mocker() as m:
            _register_mocks(m)
            results = self.crawler.get_results(SEARCH_URL)
        addresses = [r["address"] for r in results]
        self.assertTrue(any("Rotfederstraße" in a for a in addresses))

    def test_rooms_parsed_from_detail(self):
        with req_mock.Mocker() as m:
            _register_mocks(m)
            results = self.crawler.get_results(SEARCH_URL)
        self.assertTrue(any(r["rooms"] for r in results))

    def test_size_parsed_from_detail(self):
        with req_mock.Mocker() as m:
            _register_mocks(m)
            results = self.crawler.get_results(SEARCH_URL)
        self.assertTrue(any(r["size"] for r in results))

    def test_price_prefers_grundmiete(self):
        with req_mock.Mocker() as m:
            _register_mocks(m)
            results = self.crawler.get_results(SEARCH_URL)
        self.assertTrue(any("1.460" in r["price"] for r in results))

    def test_image_from_og_tag(self):
        with req_mock.Mocker() as m:
            _register_mocks(m)
            results = self.crawler.get_results(SEARCH_URL)
        self.assertTrue(any(r["image"] for r in results))

    def test_detail_404_excluded(self):
        with req_mock.Mocker() as m:
            _register_mocks(m, detail_status=404)
            results = self.crawler.get_results(SEARCH_URL)
        self.assertEqual(results, [])

    def test_detail_5xx_retries_then_succeeds(self):
        """5xx server error should trigger one retry — second response succeeds."""
        items = _index()
        first_apt = next(i for i in items if "Stellplatz" not in i["title"]["rendered"])
        with req_mock.Mocker() as m:
            m.register_uri(req_mock.GET, WP_API_URL, [
                {"json": items, "status_code": 200},
                {"json": [], "status_code": 200},
            ])
            for item in items:
                if item is first_apt:
                    m.register_uri(req_mock.GET, item["link"], [
                        {"text": "", "status_code": 503},
                        {"text": _detail_html(), "status_code": 200},
                    ])
                else:
                    m.register_uri(req_mock.GET, item["link"], text=_detail_html())
            results = self.crawler.get_results(SEARCH_URL)
        self.assertTrue(any(r["url"] == first_apt["link"] for r in results))

    def test_index_connection_error_returns_empty(self):
        with req_mock.Mocker() as m:
            m.get(WP_API_URL, exc=requests.exceptions.ConnectionError("refused"))
            results = self.crawler.get_results(SEARCH_URL)
        self.assertEqual(results, [])

    def test_index_non_200_returns_empty(self):
        with req_mock.Mocker() as m:
            m.get(WP_API_URL, status_code=503)
            results = self.crawler.get_results(SEARCH_URL)
        self.assertEqual(results, [])

    def test_max_pages_respected(self):
        big_index = [_index()[0]] * 100  # simulate full page
        extra_item = {
            "id": 999999,
            "link": "https://www.gewobag.de/fuer-mietinteressentinnen/mietangebote/999999/",
            "title": {"rendered": "Wohnung page 2"},
        }
        with req_mock.Mocker() as m:
            m.register_uri(req_mock.GET, WP_API_URL, [
                {"json": big_index, "status_code": 200},
                {"json": [extra_item], "status_code": 200},
                {"json": [], "status_code": 200},
            ])
            for item in big_index:
                m.register_uri(req_mock.GET, item["link"], text=_detail_html())
            m.register_uri(req_mock.GET, extra_item["link"], text=_detail_html())
            results_p1 = self.crawler.get_results(SEARCH_URL, max_pages=1)
            results_p2 = self.crawler.get_results(SEARCH_URL, max_pages=2)
        self.assertLessEqual(len(results_p1), 100)

    def test_url_pattern_matches(self):
        self.assertIsNotNone(self.crawler.URL_PATTERN.match(SEARCH_URL))

    def test_url_pattern_no_match(self):
        self.assertIsNone(self.crawler.URL_PATTERN.match("https://www.wbm.de/"))

    def test_crawler_name_is_string(self):
        self.assertIsInstance(self.crawler.get_name(), str)

    def test_extract_data_raises(self):
        with self.assertRaises(NotImplementedError):
            self.crawler.extract_data(None)

    def test_is_apartment_filters_stellplatz(self):
        self.assertFalse(self.crawler._is_apartment({"title": {"rendered": "Stellplatz im Freien"}}))

    def test_is_apartment_keeps_wohnung(self):
        self.assertTrue(self.crawler._is_apartment({"title": {"rendered": "Schöne 2-Zimmer-Wohnung"}}))

    def test_is_apartment_filters_garage(self):
        self.assertFalse(self.crawler._is_apartment({"title": {"rendered": "Garage in Mitte"}}))

    def test_is_apartment_filters_plural_stellplaetze(self):
        # Plural with umlaut: ä→ae makes "Stellplätze" → "stellplaetze".
        # Both stems "stellplatz" and "stellplaetz" must be in the keyword set.
        self.assertFalse(self.crawler._is_apartment(
            {"title": {"rendered": "Offene Stellplätze im Freien zu vermieten"}}
        ))

    def test_is_apartment_filters_parkplatz(self):
        self.assertFalse(self.crawler._is_apartment(
            {"title": {"rendered": "Sie haben die Parkplatzsuche satt?"}}
        ))

    def test_is_apartment_filters_behindertenparkplatz(self):
        self.assertFalse(self.crawler._is_apartment(
            {"title": {"rendered": "Offener Behindertenparkplatz im Freien"}}
        ))

    def test_is_apartment_filters_tiefgarage(self):
        self.assertFalse(self.crawler._is_apartment(
            {"title": {"rendered": "Stellplatz in der Tiefgarage Daumstr. 123"}}
        ))

    def test_is_apartment_keeps_neubau_with_umlaut(self):
        # Real-world title that should pass — contains umlauts but is an apartment.
        self.assertTrue(self.crawler._is_apartment(
            {"title": {"rendered": "Geräumiger Neubau mit Fußbodenheizung"}}
        ))

    def test_is_apartment_filters_fahrradkeller(self):
        self.assertFalse(self.crawler._is_apartment(
            {"title": {"rendered": "Mofa- Fahrradkeller!"}}
        ))


if __name__ == "__main__":
    unittest.main()
