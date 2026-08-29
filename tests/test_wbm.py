"""Tests for WBM crawler"""
import os
import unittest

import requests
import requests_mock as req_mock
from bs4 import BeautifulSoup

from berlin_flat_hunter.crawlers.wbm import Wbm

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "wbm_listings.html")
SEARCH_URL = "https://www.wbm.de/wohnungen-berlin/angebote/"


class FakeConfig:
    def captcha_enabled(self):
        return False
    def use_proxy(self):
        return False


def _fixture_html():
    with open(FIXTURE) as f:
        return f.read()


class TestWbmCrawler(unittest.TestCase):

    def setUp(self):
        self.crawler = Wbm(FakeConfig())

    def test_get_results_returns_list(self):
        with req_mock.Mocker() as m:
            m.get(req_mock.ANY, text=_fixture_html())
            results = self.crawler.get_results(SEARCH_URL, max_pages=1)
        self.assertIsInstance(results, list)

    def test_get_results_parses_two_entries(self):
        with req_mock.Mocker() as m:
            m.get(req_mock.ANY, text=_fixture_html())
            results = self.crawler.get_results(SEARCH_URL, max_pages=1)
        self.assertEqual(len(results), 2)

    def test_entry_has_required_keys(self):
        with req_mock.Mocker() as m:
            m.get(req_mock.ANY, text=_fixture_html())
            results = self.crawler.get_results(SEARCH_URL, max_pages=1)
        for key in ("id", "url", "title", "address", "rooms", "size", "price", "crawler"):
            self.assertIn(key, results[0])

    def test_id_is_stable_int_from_url_slug(self):
        # WBM's data-uid is unstable (changes for the same listing over time), so
        # identity is a stable crc32 of the URL slug — same URL => same id, and it
        # is a plain int for flathunter's int(id) dedup.
        import zlib

        with req_mock.Mocker() as m:
            m.get(req_mock.ANY, text=_fixture_html())
            results = self.crawler.get_results(SEARCH_URL, max_pages=1)

        for r in results:
            slug = r["url"].rstrip("/").rsplit("/", 1)[-1]
            self.assertEqual(r["id"], zlib.crc32(slug.encode("utf-8")))
            self.assertIsInstance(r["id"], int)
        # distinct listings get distinct ids
        self.assertNotEqual(results[0]["id"], results[1]["id"])

    def test_entry_url_is_absolute(self):
        with req_mock.Mocker() as m:
            m.get(req_mock.ANY, text=_fixture_html())
            results = self.crawler.get_results(SEARCH_URL, max_pages=1)
        self.assertTrue(results[0]["url"].startswith("https://"))

    def test_entry_url_contains_wbm(self):
        with req_mock.Mocker() as m:
            m.get(req_mock.ANY, text=_fixture_html())
            results = self.crawler.get_results(SEARCH_URL, max_pages=1)
        self.assertIn("wbm.de", results[0]["url"])

    def test_entry_title_from_imagetitle(self):
        with req_mock.Mocker() as m:
            m.get(req_mock.ANY, text=_fixture_html())
            results = self.crawler.get_results(SEARCH_URL, max_pages=1)
        self.assertIn("Spandau", results[0]["title"])

    def test_entry_address_from_address_div(self):
        with req_mock.Mocker() as m:
            m.get(req_mock.ANY, text=_fixture_html())
            results = self.crawler.get_results(SEARCH_URL, max_pages=1)
        self.assertIn("Eidechsenweg", results[0]["address"])

    def test_entry_price_from_main_property_rent(self):
        with req_mock.Mocker() as m:
            m.get(req_mock.ANY, text=_fixture_html())
            results = self.crawler.get_results(SEARCH_URL, max_pages=1)
        self.assertIn("1.112,13", results[0]["price"])

    def test_entry_size_from_main_property_size(self):
        with req_mock.Mocker() as m:
            m.get(req_mock.ANY, text=_fixture_html())
            results = self.crawler.get_results(SEARCH_URL, max_pages=1)
        self.assertIn("71,75", results[0]["size"])

    def test_entry_rooms_from_main_property_rooms(self):
        with req_mock.Mocker() as m:
            m.get(req_mock.ANY, text=_fixture_html())
            results = self.crawler.get_results(SEARCH_URL, max_pages=1)
        self.assertEqual(results[0]["rooms"], "3")

    def test_image_from_imgwrap_data_src(self):
        with req_mock.Mocker() as m:
            m.get(req_mock.ANY, text=_fixture_html())
            results = self.crawler.get_results(SEARCH_URL, max_pages=1)
        self.assertIn("wbm.de", results[0]["image"])

    def test_missing_image_is_empty_string(self):
        with req_mock.Mocker() as m:
            m.get(req_mock.ANY, text=_fixture_html())
            results = self.crawler.get_results(SEARCH_URL, max_pages=1)
        self.assertEqual(results[1]["image"], "")

    def test_extract_data_on_empty_soup(self):
        soup = BeautifulSoup("<html></html>", "html.parser")
        self.assertEqual(self.crawler.extract_data(soup), [])

    def test_has_next_detects_pagination(self):
        soup = BeautifulSoup(_fixture_html(), "html.parser")
        self.assertTrue(self.crawler._has_next(soup, 1))

    def test_has_next_false_on_last_page(self):
        soup = BeautifulSoup(_fixture_html(), "html.parser")
        self.assertFalse(self.crawler._has_next(soup, 2))

    def test_has_next_false_without_pagination(self):
        self.assertFalse(self.crawler._has_next(BeautifulSoup("<html></html>", "html.parser"), 1))

    def test_has_next_does_not_match_substring_page_number(self):
        """page=2 must NOT match page=20 — regression for substring-match bug."""
        html = (
            '<ul class="pagination">'
            '<li><a href="?page=20">20</a></li>'
            '<li><a href="?page=21">21</a></li>'
            '</ul>'
        )
        soup = BeautifulSoup(html, "html.parser")
        self.assertFalse(self.crawler._has_next(soup, 1))  # looking for page=2
        self.assertTrue(self.crawler._has_next(soup, 19))  # looking for page=20

    def test_different_ids_for_different_items(self):
        with req_mock.Mocker() as m:
            m.get(req_mock.ANY, text=_fixture_html())
            results = self.crawler.get_results(SEARCH_URL, max_pages=1)
        self.assertNotEqual(results[0]["id"], results[1]["id"])

    def test_url_pattern_matches(self):
        self.assertIsNotNone(self.crawler.URL_PATTERN.match(SEARCH_URL))

    def test_url_pattern_no_match(self):
        self.assertIsNone(self.crawler.URL_PATTERN.match("https://www.gewobag.de/"))

    def test_crawler_name_is_string(self):
        self.assertIsInstance(self.crawler.get_name(), str)

    def test_item_without_data_uid_skipped(self):
        html = '<html><body><div class="openimmo-search-list-item"><h2 class="imageTitle">Test</h2></div></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        self.assertEqual(self.crawler.extract_data(soup), [])

    def test_site_reported_empty_via_class(self):
        """Container carries the ``empty`` class → site says 'no listings'."""
        html = '<html><body><div class="tx-openimmo search bootstrap empty"></div></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        self.assertTrue(self.crawler._is_site_reported_empty(soup))

    def test_site_reported_empty_via_no_offer_block(self):
        html = '<html><body><div class="openimmo-no-offer-avilable"><p>Leider…</p></div></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        self.assertTrue(self.crawler._is_site_reported_empty(soup))

    def test_site_reported_empty_false_on_populated_page(self):
        soup = BeautifulSoup(_fixture_html(), "html.parser")
        self.assertFalse(self.crawler._is_site_reported_empty(soup))

    def test_get_results_sets_flag_on_empty_state(self):
        empty_html = ('<html><body><div class="tx-openimmo search bootstrap empty">'
                      '<div class="openimmo-no-offer-avilable">nope</div></div></body></html>')
        with req_mock.Mocker() as m:
            m.get(req_mock.ANY, text=empty_html)
            results = self.crawler.get_results(SEARCH_URL, max_pages=1)
        self.assertEqual(results, [])
        self.assertTrue(self.crawler.last_crawl_site_reported_empty)

    def test_get_results_clears_flag_on_populated_page(self):
        self.crawler.last_crawl_site_reported_empty = True  # simulate stale flag
        with req_mock.Mocker() as m:
            m.get(req_mock.ANY, text=_fixture_html())
            results = self.crawler.get_results(SEARCH_URL, max_pages=1)
        self.assertGreater(len(results), 0)
        self.assertFalse(self.crawler.last_crawl_site_reported_empty)


if __name__ == "__main__":
    unittest.main()
