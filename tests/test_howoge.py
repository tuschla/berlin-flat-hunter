"""Tests for Howoge crawler"""
import json
import os
import unittest
from urllib.parse import parse_qs

import requests
import requests_mock as req_mock
from bs4 import BeautifulSoup

from berlin_flat_hunter.crawlers import howoge
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

    def test_5xx_retries_then_succeeds(self):
        """A single transient 5xx must trigger one retry — second response wins."""
        items = _payload()
        empty = {"immocount": 0, "teasercount": 0, "immoobjects": []}
        with req_mock.Mocker() as m:
            m.register_uri(req_mock.POST, JSON_ENDPOINT, [
                {"text": "", "status_code": 503},
                {"json": items, "status_code": 200},
                {"json": empty, "status_code": 200},
            ])
            results = self.crawler.get_results(SEARCH_URL)
        self.assertEqual(len(results), 2)

    def test_5xx_persistent_returns_empty(self):
        """Both attempts 5xx → give up, return empty (not raise)."""
        with req_mock.Mocker() as m:
            m.register_uri(req_mock.POST, JSON_ENDPOINT, [
                {"text": "", "status_code": 503},
                {"text": "", "status_code": 503},
            ])
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


def _items(first_uid: int, n: int) -> list[dict]:
    return [{"uid": first_uid + i, "link": f"/wohnung/{first_uid + i}.html",
             "title": "Teststraße 1, 10115 Berlin", "rooms": 2, "area": 50, "rent": 600}
            for i in range(n)]


def _page_of(request) -> int:
    return int(parse_qs(request.text)["tx_howrealestate_json_list[page]"][0])


class TestHowogePagination(unittest.TestCase):
    """The live endpoint ignores page/limit and returns the full list on every
    page; with ≥ 50 listings the crawler used to re-fetch it forever."""

    def setUp(self):
        self.crawler = Howoge(FakeConfig())

    def _crawl(self, respond):
        with req_mock.Mocker() as m:
            m.post(JSON_ENDPOINT, json=lambda request, context: {
                "immoobjects": respond(_page_of(request))})
            results = self.crawler.get_results(SEARCH_URL)
            return results, m.call_count

    def test_page_ignoring_endpoint_terminates_at_and_above_page_size(self):
        for n in (50, 60):
            with self.subTest(listings=n):
                results, calls = self._crawl(lambda page, n=n: _items(1, n))
                self.assertEqual(len(results), n)
                self.assertEqual(calls, 2)

    def test_below_page_size_needs_one_request(self):
        results, calls = self._crawl(lambda page: _items(1, 49))
        self.assertEqual(len(results), 49)
        self.assertEqual(calls, 1)

    def test_real_pagination_still_collects_every_page(self):
        pages = {1: _items(1, 50), 2: _items(51, 10)}
        results, calls = self._crawl(lambda page: pages.get(page, []))
        self.assertEqual(sorted(r["id"] for r in results), list(range(1, 61)))
        self.assertEqual(calls, 2)

    def test_runaway_pagination_stops_at_page_cap(self):
        # Every page full of never-seen listings: only the cap can stop it.
        results, calls = self._crawl(lambda page: _items(page * 1000, 50))
        self.assertEqual(calls, howoge._MAX_PAGES)
        self.assertEqual(len(results), 50 * howoge._MAX_PAGES)



if __name__ == "__main__":
    unittest.main()
