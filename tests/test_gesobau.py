"""Tests for Gesobau crawler"""
import os
import unittest

import requests_mock as req_mock
from bs4 import BeautifulSoup

from berlin_flat_hunter.crawlers.gesobau import Gesobau

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "gesobau_listings.html")
SEARCH_URL = "https://www.gesobau.de/mieten/wohnungssuche.html"


class FakeConfig:
    def captcha_enabled(self):
        return False
    def use_proxy(self):
        return False


def _fixture_html():
    with open(FIXTURE) as f:
        return f.read()


class TestGesobauCrawler(unittest.TestCase):

    def setUp(self):
        self.crawler = Gesobau(FakeConfig())

    def test_get_results_returns_list(self):
        with req_mock.Mocker() as m:
            m.get(SEARCH_URL, text=_fixture_html())
            results = self.crawler.get_results(SEARCH_URL)
        self.assertIsInstance(results, list)

    def test_parses_two_entries(self):
        with req_mock.Mocker() as m:
            m.get(SEARCH_URL, text=_fixture_html())
            results = self.crawler.get_results(SEARCH_URL)
        self.assertEqual(len(results), 2)

    def test_required_keys_present(self):
        with req_mock.Mocker() as m:
            m.get(SEARCH_URL, text=_fixture_html())
            results = self.crawler.get_results(SEARCH_URL)
        for key in ("id", "url", "title", "address", "rooms", "size", "price", "crawler"):
            self.assertIn(key, results[0])

    def test_id_from_data_uid(self):
        with req_mock.Mocker() as m:
            m.get(SEARCH_URL, text=_fixture_html())
            results = self.crawler.get_results(SEARCH_URL)
        self.assertEqual(results[0]["id"], 12360)
        self.assertEqual(results[1]["id"], 12361)

    def test_url_absolute(self):
        with req_mock.Mocker() as m:
            m.get(SEARCH_URL, text=_fixture_html())
            results = self.crawler.get_results(SEARCH_URL)
        self.assertTrue(results[0]["url"].startswith("https://www.gesobau.de"))

    def test_address_combines_street_and_district(self):
        with req_mock.Mocker() as m:
            m.get(SEARCH_URL, text=_fixture_html())
            results = self.crawler.get_results(SEARCH_URL)
        self.assertIn("Gerichtstraße", results[0]["address"])
        self.assertIn("Gesundbrunnen", results[0]["address"])

    def test_price_extracted(self):
        with req_mock.Mocker() as m:
            m.get(SEARCH_URL, text=_fixture_html())
            results = self.crawler.get_results(SEARCH_URL)
        self.assertIn("2.344", results[0]["price"])

    def test_rooms_extracted(self):
        with req_mock.Mocker() as m:
            m.get(SEARCH_URL, text=_fixture_html())
            results = self.crawler.get_results(SEARCH_URL)
        self.assertIn("Zimmer", results[0]["rooms"])

    def test_size_extracted(self):
        with req_mock.Mocker() as m:
            m.get(SEARCH_URL, text=_fixture_html())
            results = self.crawler.get_results(SEARCH_URL)
        self.assertIn("m²", results[0]["size"])

    def test_image_extracted(self):
        with req_mock.Mocker() as m:
            m.get(SEARCH_URL, text=_fixture_html())
            results = self.crawler.get_results(SEARCH_URL)
        self.assertIn("listing-1.jpg", results[0]["image"])

    def test_extract_data_on_empty_soup(self):
        self.assertEqual(
            self.crawler.extract_data(BeautifulSoup("<html></html>", "html.parser")),
            [],
        )

    def test_url_pattern_matches(self):
        self.assertIsNotNone(self.crawler.URL_PATTERN.match(SEARCH_URL))

    def test_url_pattern_no_match(self):
        self.assertIsNone(self.crawler.URL_PATTERN.match("https://www.gewobag.de/"))

    def test_item_without_uid_skipped(self):
        html = '<html><body><article class="apartment"><h3 class="basicTeaser__title"><a href="/x">T</a></h3></article></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        self.assertEqual(self.crawler.extract_data(soup), [])


if __name__ == "__main__":
    unittest.main()
