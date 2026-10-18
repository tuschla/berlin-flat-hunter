"""Tests for Gesobau crawler"""
import os
import unittest

import requests_mock as req_mock
from bs4 import BeautifulSoup

from berlin_flat_hunter.crawlers import gesobau
from berlin_flat_hunter.crawlers.common import listing_id
from berlin_flat_hunter.crawlers.gesobau import Gesobau

_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
FIXTURE = os.path.join(_FIXTURES, "gesobau_listings.html")
# Captured 2026-10-18 from https://www.gesobau.de/mieten/wohnungssuche/ and its
# page-2 link (``?tx_solr%5Bpage%5D=2``).
SEARCH_P1 = os.path.join(_FIXTURES, "gesobau_search_p1.html")
SEARCH_P2 = os.path.join(_FIXTURES, "gesobau_search_p2.html")
SEARCH_URL = "https://www.gesobau.de/mieten/wohnungssuche.html"
LIVE_URL = "https://www.gesobau.de/mieten/wohnungssuche/"
PAGE2_URL = "https://www.gesobau.de/mieten/wohnungssuche/?tx_solr%5Bpage%5D=2"


class FakeConfig:
    def captcha_enabled(self):
        return False
    def use_proxy(self):
        return False


def _fixture_html():
    with open(FIXTURE) as f:
        return f.read()


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _card(n, href, price="1.000,00 €"):
    return (f'<article class="basicTeaser apartment" data-apartment-uid="{n}">'
            f'<h3 class="basicTeaser__title"><a href="{href}">T{n}</a></h3>'
            f'<div class="apartment__info"><span>{price}</span><span>2 Zimmer</span>'
            f'<span>50 m²</span></div></article>')


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

    def test_id_is_listing_id_of_url(self):
        with req_mock.Mocker() as m:
            m.get(SEARCH_URL, text=_fixture_html())
            results = self.crawler.get_results(SEARCH_URL)
        for r in results:
            self.assertEqual(r["id"], listing_id(r["url"]))
        self.assertNotEqual(results[0]["id"], results[1]["id"])

    def test_id_stable_when_site_uid_changes(self):
        changed = _fixture_html().replace('"12360"', '"99990"').replace('"12361"', '"99991"')
        with req_mock.Mocker() as m:
            m.get(SEARCH_URL, [{"text": _fixture_html()}, {"text": changed}])
            before = [r["id"] for r in self.crawler.get_results(SEARCH_URL)]
            after = [r["id"] for r in self.crawler.get_results(SEARCH_URL)]
        self.assertEqual(before, after)

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
        # The card's € figure is "Miete inkl. NK" (warm rent).
        self.assertEqual(results[0]["price_warm"], results[0]["price"])
        self.assertEqual(results[0]["price_cold"], "")

    def test_price_without_amount_is_empty(self):
        html = f"<html><body>{_card(1, '/d/a/', price='Auf Anfrage €')}</body></html>"
        entries = self.crawler.extract_data(BeautifulSoup(html, "html.parser"))
        self.assertEqual(entries[0]["price"], "")
        self.assertEqual(entries[0]["price_warm"], "")

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
        # Promo teasers (Neubau, Wohnen im Alter) are article.apartment too but
        # carry no data-apartment-uid.
        html = '<html><body><article class="apartment"><h3 class="basicTeaser__title"><a href="/x">T</a></h3></article></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        self.assertEqual(self.crawler.extract_data(soup), [])


class TestGesobauPagination(unittest.TestCase):

    def setUp(self):
        self.crawler = Gesobau(FakeConfig())

    def _crawl(self, page2_html, max_pages=None):
        with req_mock.Mocker() as m:
            m.get(LIVE_URL, text=_read(SEARCH_P1), complete_qs=True)
            m.get(PAGE2_URL, text=page2_html, complete_qs=True)
            results = self.crawler.get_results(LIVE_URL, max_pages=max_pages)
            return results, [r.url for r in m.request_history]

    def test_follows_tx_solr_link_to_page_2(self):
        results, urls = self._crawl(_read(SEARCH_P2))
        # Page 2 only links back to page 1, so the crawl stops there.
        self.assertEqual(urls, [LIVE_URL, PAGE2_URL])
        self.assertEqual(len(results), 11)
        self.assertEqual(len({r["id"] for r in results}), 11)
        for r in results:
            self.assertIn("/mieten/wohnungssuche/detailseite/", r["url"])
            self.assertEqual(r["id"], listing_id(r["url"]))

    def test_live_card_fields(self):
        results, _ = self._crawl(_read(SEARCH_P2))
        first = results[0]
        self.assertEqual(first["price"], "1.478,09 €")
        self.assertEqual(first["price_warm"], "1.478,09 €")
        self.assertEqual(first["rooms"], "2 Zimmer")
        self.assertEqual(first["size"], "57,98 m²")
        self.assertEqual(first["address"], "Grüntaler Straße 50B, 13359 Berlin, Gesundbrunnen")

    def test_stops_when_page_yields_nothing_new(self):
        # Page 2 replaying page 1 (which still links to page 2) must not loop.
        results, urls = self._crawl(_read(SEARCH_P1))
        self.assertEqual(urls, [LIVE_URL, PAGE2_URL])
        self.assertEqual(len(results), 6)

    def test_max_pages_respected(self):
        results, urls = self._crawl(_read(SEARCH_P2), max_pages=1)
        self.assertEqual(urls, [LIVE_URL])
        self.assertEqual(len(results), 6)

    def test_unencoded_page_link_followed(self):
        p1 = (f"<html><body>{_card(1, '/d/a/')}"
              '<a class="page-link" href="/s/?tx_solr[page]=2">2</a></body></html>')
        p2 = f"<html><body>{_card(2, '/d/b/')}</body></html>"
        with req_mock.Mocker() as m:
            m.get("https://www.gesobau.de/s/", text=p1, complete_qs=True)
            m.get("https://www.gesobau.de/s/?tx_solr[page]=2", text=p2, complete_qs=True)
            results = self.crawler.get_results("https://www.gesobau.de/s/")
        self.assertEqual([r["url"] for r in results],
                         ["https://www.gesobau.de/d/a/", "https://www.gesobau.de/d/b/"])

    def test_runaway_pagination_stops_at_page_cap(self):
        def page(request, context):
            n = int(request.qs.get("tx_solr[page]", ["1"])[0])
            return (f"<html><body>{_card(n, f'/d/{n}/')}"
                    f'<a class="page-link" href="/s/?tx_solr%5Bpage%5D={n + 1}">next</a>'
                    "</body></html>")
        with req_mock.Mocker() as m:
            m.get(req_mock.ANY, text=page)
            with self.assertLogs("flathunt", level="WARNING") as cm:
                results = self.crawler.get_results("https://www.gesobau.de/s/")
            calls = m.call_count
        self.assertEqual(calls, gesobau._MAX_PAGES)
        self.assertEqual(len(results), gesobau._MAX_PAGES)
        self.assertTrue(any("page cap" in line for line in cm.output))


if __name__ == "__main__":
    unittest.main()
