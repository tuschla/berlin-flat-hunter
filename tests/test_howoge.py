"""Tests for Howoge crawler"""
import json
import os
import unittest

import requests
import requests_mock as req_mock
from bs4 import BeautifulSoup

from berlin_flat_hunter.crawlers.howoge import Howoge, JSON_ENDPOINT

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "howoge_index.json")
SEARCH_URL = "https://www.howoge.de/immobiliensuche/wohnungssuche.html"


class FakeConfig:
    def captcha_enabled(self):
        return False
    def use_proxy(self):
        return False


def _payload():
    with open(FIXTURE) as f:
        return json.load(f)


def _register(m, payload=None, status_code=200):
    """Register the JSON endpoint to return ``payload`` on the first call and an
    empty list on every subsequent call (so pagination terminates)."""
    items = payload if payload is not None else _payload()
    empty = {"immocount": 0, "teasercount": 0, "immoobjects": []}
    m.register_uri(
        req_mock.POST, JSON_ENDPOINT,
        [
            {"json": items, "status_code": status_code},
            {"json": empty, "status_code": 200},
        ],
    )


class TestHowogeCrawler(unittest.TestCase):

    def setUp(self):
        self.crawler = Howoge(FakeConfig())

    def test_returns_list(self):
        with req_mock.Mocker() as m:
            _register(m)
            results = self.crawler.get_results(SEARCH_URL)
        self.assertIsInstance(results, list)

    def test_two_entries_parsed(self):
        with req_mock.Mocker() as m:
            _register(m)
            results = self.crawler.get_results(SEARCH_URL)
        self.assertEqual(len(results), 2)

    def test_required_keys_present(self):
        with req_mock.Mocker() as m:
            _register(m)
            results = self.crawler.get_results(SEARCH_URL)
        for key in ("id", "url", "title", "address", "rooms", "size", "price",
                    "image", "crawler"):
            self.assertIn(key, results[0])

    def test_id_is_int(self):
        with req_mock.Mocker() as m:
            _register(m)
            results = self.crawler.get_results(SEARCH_URL)
        self.assertEqual(results[0]["id"], 7094)
        self.assertIsInstance(results[0]["id"], int)

    def test_url_absolute(self):
        with req_mock.Mocker() as m:
            _register(m)
            results = self.crawler.get_results(SEARCH_URL)
        self.assertTrue(results[0]["url"].startswith("https://www.howoge.de/"))

    def test_image_absolute(self):
        with req_mock.Mocker() as m:
            _register(m)
            results = self.crawler.get_results(SEARCH_URL)
        self.assertTrue(results[0]["image"].startswith("https://www.howoge.de/"))

    def test_address_contains_plz(self):
        # plz_filter greps PLZ out of address — keep the API stable.
        with req_mock.Mocker() as m:
            _register(m)
            results = self.crawler.get_results(SEARCH_URL)
        self.assertIn("13587", results[0]["address"])
        self.assertIn("Hakenfelde", results[0]["address"])

    def test_title_uses_notice_when_present(self):
        with req_mock.Mocker() as m:
            _register(m)
            results = self.crawler.get_results(SEARCH_URL)
        self.assertEqual(results[0]["title"], "3-Zimmer-Wohnung (WBS 100-140)")

    def test_title_synthesized_when_notice_missing(self):
        with req_mock.Mocker() as m:
            _register(m)
            results = self.crawler.get_results(SEARCH_URL)
        # Second item has empty notice — title should be synthesized from rooms+district.
        self.assertIn("Friedrichshain", results[1]["title"])
        self.assertIn("Zimmer", results[1]["title"])

    def test_price_formatted(self):
        with req_mock.Mocker() as m:
            _register(m)
            results = self.crawler.get_results(SEARCH_URL)
        self.assertIn("803", results[0]["price"])
        self.assertIn("€", results[0]["price"])

    def test_rooms_formatted(self):
        with req_mock.Mocker() as m:
            _register(m)
            results = self.crawler.get_results(SEARCH_URL)
        self.assertIn("Zimmer", results[0]["rooms"])

    def test_size_formatted_int(self):
        with req_mock.Mocker() as m:
            _register(m)
            results = self.crawler.get_results(SEARCH_URL)
        self.assertEqual(results[0]["size"], "73 m²")

    def test_size_formatted_float(self):
        with req_mock.Mocker() as m:
            _register(m)
            results = self.crawler.get_results(SEARCH_URL)
        # Second item area is 55.5 — should keep 2 decimals.
        self.assertIn("55", results[1]["size"])
        self.assertIn("m²", results[1]["size"])

    def test_url_pattern_matches(self):
        self.assertIsNotNone(self.crawler.URL_PATTERN.match(SEARCH_URL))

    def test_url_pattern_no_match(self):
        self.assertIsNone(self.crawler.URL_PATTERN.match("https://www.gewobag.de/"))

    def test_request_failure_returns_empty(self):
        with req_mock.Mocker() as m:
            m.post(JSON_ENDPOINT, exc=requests.exceptions.ConnectionError("refused"))
            results = self.crawler.get_results(SEARCH_URL)
        self.assertEqual(results, [])

    def test_non_200_returns_empty(self):
        with req_mock.Mocker() as m:
            m.post(JSON_ENDPOINT, status_code=503)
            results = self.crawler.get_results(SEARCH_URL)
        self.assertEqual(results, [])

    def test_invalid_json_returns_empty(self):
        with req_mock.Mocker() as m:
            m.post(JSON_ENDPOINT, text="not json")
            results = self.crawler.get_results(SEARCH_URL)
        self.assertEqual(results, [])

    def test_item_without_uid_skipped(self):
        bad = {"immocount": 1, "teasercount": 0,
               "immoobjects": [{"link": "/x.html", "title": "Foo"}]}
        with req_mock.Mocker() as m:
            _register(m, payload=bad)
            results = self.crawler.get_results(SEARCH_URL)
        self.assertEqual(results, [])

    def test_item_without_link_skipped(self):
        bad = {"immocount": 1, "teasercount": 0,
               "immoobjects": [{"uid": 1, "title": "Foo"}]}
        with req_mock.Mocker() as m:
            _register(m, payload=bad)
            results = self.crawler.get_results(SEARCH_URL)
        self.assertEqual(results, [])

    def test_extract_data_returns_empty(self):
        # JSON crawler does not use HTML soup path.
        self.assertEqual(
            self.crawler.extract_data(BeautifulSoup("<html></html>", "html.parser")),
            [],
        )

    def test_crawler_name_is_string(self):
        self.assertIsInstance(self.crawler.get_name(), str)


if __name__ == "__main__":
    unittest.main()
