"""Tests for the degewo crawler — requests-mocked HTML scrape."""
import os
import unittest

import requests_mock as req_mock
from bs4 import BeautifulSoup

from berlin_flat_hunter.crawlers.common import SESSION, listing_id
from berlin_flat_hunter.crawlers.degewo import Degewo

SEARCH_URL = "https://www.degewo.de/immosuche/"

_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
# Captured 2026-10-18 from https://www.degewo.de/immosuche/ (default sort, page 1),
# its "Warmmiete" sort href (page 1) and that sort's page-2 href.
SEARCH_P1 = os.path.join(_FIXTURES, "degewo_search_p1.html")
WARM_P1 = os.path.join(_FIXTURES, "degewo_warmmiete_p1.html")
WARM_P2 = os.path.join(_FIXTURES, "degewo_warmmiete_p2.html")
# The signed hrefs as they appear in those pages (fragment dropped).
WARM_SORT_URL = ("https://www.degewo.de/immosuche?tx_openimmo_immobilie%5Bsearch%5D=restore"
                 "&tx_openimmo_immobilie%5BsortBy%5D=immobilie_preise_warmmiete"
                 "&cHash=18b6f2b945a4df28f5101ca5453c6979")
WARM_PAGE2_URL = ("https://www.degewo.de/immosuche?tx_openimmo_immobilie%5Bpage%5D=2"
                  "&tx_openimmo_immobilie%5Bsearch%5D=paginate"
                  "&tx_openimmo_immobilie%5BsortBy%5D=immobilie_preise_warmmiete"
                  "&cHash=b2962639e742b72fc4ab3f63557b42f5")
WARM_PAGE3_URL = ("https://www.degewo.de/immosuche?tx_openimmo_immobilie%5Bpage%5D=3"
                  "&tx_openimmo_immobilie%5Bsearch%5D=paginate"
                  "&tx_openimmo_immobilie%5BsortBy%5D=immobilie_preise_warmmiete"
                  "&cHash=d9c08e9b884280728065e24266ce5490")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _card(uid, href, title, warm, rooms, sqm, address, img):
    return f"""
    <article class="c-teaser c-teaser--apartment">
      <a class="c-img-container" href="{href}">
        <img class="c-img" src="{img}" alt="">
      </a>
      <span data-openimmo-bookmark-item-uid="{uid}"></span>
      <div class="c-teaser__content">
        <h3 class="c-headline"><a class="c-headline--linked" href="{href}">{title}</a></h3>
        <div class="c-copy"><p>{address}</p></div>
        <dl class="c-definition-list">
          <div class="c-definition-list__item">
            <dt class="c-definition-list__term">{warm}</dt>
            <dd class="c-definition-list__definition">Warmmiete</dd>
          </div>
          <div class="c-definition-list__item">
            <dt class="c-definition-list__term">{rooms}</dt>
            <dd class="c-definition-list__definition">Zimmer</dd>
          </div>
          <div class="c-definition-list__item">
            <dt class="c-definition-list__term">{sqm}</dt>
            <dd class="c-definition-list__definition">m²</dd>
          </div>
          <div class="c-definition-list__item">
            <dt class="c-definition-list__term">01.09.2026</dt>
            <dd class="c-definition-list__definition">frei ab</dd>
          </div>
        </dl>
      </div>
    </article>
    """


FIXTURE = f"""
<html><body>
<div class="article-list">
  {_card("100123", "/immosuche/details/wohnung-eins/", "3-Zimmer-Wohnung in Marzahn",
         "812,00 &euro;", "3", "72,50", "Musterstraße 1, 12679 Berlin",
         "/fileadmin/img/eins.jpg")}
  {_card("100456", "/immosuche/details/wohnung-zwei/", "2-Zimmer-Wohnung in Spandau",
         "645,30 &euro;", "2", "55", "Beispielweg 2, 13583 Berlin",
         "https://www.degewo.de/fileadmin/img/zwei.jpg")}
</div>
</body></html>
"""

EMPTY_FIXTURE = "<html><body><div class='article-list'></div></body></html>"

URL_EINS = "https://www.degewo.de/immosuche/details/wohnung-eins/"
URL_ZWEI = "https://www.degewo.de/immosuche/details/wohnung-zwei/"


def _by_url(entries, url):
    return next(e for e in entries if e["url"] == url)


class FakeConfig:
    def captcha_enabled(self): return False
    def use_proxy(self): return False
    def get_driver(self): return None


class TestDegewoCrawler(unittest.TestCase):

    def setUp(self):
        self.crawler = Degewo(FakeConfig())

    def _get(self, fixture, status_code=200):
        with req_mock.Mocker() as m:
            m.get(SEARCH_URL, text=fixture, status_code=status_code)
            return self.crawler.get_results(SEARCH_URL, max_pages=1)

    def test_parses_all_cards(self):
        entries = self._get(FIXTURE)
        self.assertEqual(len(entries), 2)

    def test_fields_parsed(self):
        entries = self._get(FIXTURE)
        first = _by_url(entries, URL_EINS)
        self.assertEqual(first["title"], "3-Zimmer-Wohnung in Marzahn")
        self.assertEqual(first["price"], "812 €")
        self.assertEqual(first["price_warm"], "812 €")
        self.assertEqual(first["price_cold"], "")
        self.assertEqual(first["rooms"], "3 Zimmer")
        self.assertEqual(first["size"], "72.50 m²")
        self.assertEqual(first["address"], "Musterstraße 1, 12679 Berlin")
        self.assertEqual(first["crawler"], "Degewo")

    def test_id_is_listing_id_of_url(self):
        entries = self._get(FIXTURE)
        for e in entries:
            self.assertEqual(e["id"], listing_id(e["url"]))

    def test_id_stable_when_bookmark_uid_changes(self):
        changed = FIXTURE.replace('"100123"', '"999999"').replace('"100456"', '"888888"')
        before = sorted(e["id"] for e in self._get(FIXTURE))
        after = sorted(e["id"] for e in self._get(changed))
        self.assertEqual(before, after)

    def test_image_absolutised(self):
        entries = self._get(FIXTURE)
        first = _by_url(entries, URL_EINS)
        second = _by_url(entries, URL_ZWEI)
        self.assertEqual(first["image"],
                         "https://www.degewo.de/fileadmin/img/eins.jpg")
        self.assertEqual(second["image"],
                         "https://www.degewo.de/fileadmin/img/zwei.jpg")

    def test_integer_size_formats_without_decimals(self):
        entries = self._get(FIXTURE)
        second = _by_url(entries, URL_ZWEI)
        self.assertEqual(second["size"], "55 m²")
        self.assertEqual(second["price"], "645.30 €")

    def test_empty_container_returns_empty(self):
        self.assertEqual(self._get(EMPTY_FIXTURE), [])

    def test_http_error_returns_empty(self):
        self.assertEqual(self._get(FIXTURE, status_code=403), [])

    def test_server_error_returns_empty(self):
        self.assertEqual(self._get(FIXTURE, status_code=500), [])

    def test_extract_data_is_noop(self):
        self.assertEqual(self.crawler.extract_data(None), [])


class TestDegewoLiveMarkup(unittest.TestCase):
    """Against captured live pages: the default sort ties listings so its pages
    overlap; the crawl restarts from page 1's own signed Warmmiete sort href."""

    def setUp(self):
        self.crawler = Degewo(FakeConfig())

    def _crawl(self, max_pages=None):
        with req_mock.Mocker() as m:
            m.get(SEARCH_URL, text=_read(SEARCH_P1), complete_qs=True)
            m.get(WARM_SORT_URL, text=_read(WARM_P1), complete_qs=True)
            m.get(WARM_PAGE2_URL, text=_read(WARM_P2), complete_qs=True)
            # Page 3 replays page 2: nothing new, so the crawl stops there.
            m.get(WARM_PAGE3_URL, text=_read(WARM_P2), complete_qs=True)
            entries = self.crawler.get_results(SEARCH_URL, max_pages=max_pages)
            return entries, m.request_history

    def test_result_count_parsed(self):
        soup = BeautifulSoup(_read(SEARCH_P1), "lxml")
        self.assertEqual(self.crawler._result_count(soup), 93)

    def test_warm_rent_sort_href_read_from_page(self):
        soup = BeautifulSoup(_read(SEARCH_P1), "lxml")
        self.assertEqual(self.crawler._warm_rent_sort_url(soup), WARM_SORT_URL)

    def test_starts_from_sort_href_and_follows_pagination(self):
        entries, history = self._crawl(max_pages=2)
        self.assertEqual([r.url for r in history], [SEARCH_URL, WARM_SORT_URL, WARM_PAGE2_URL])
        self.assertEqual(len(entries), 20)
        self.assertEqual(len({e["id"] for e in entries}), 20)

    def test_ids_are_listing_id_of_url(self):
        entries, _ = self._crawl(max_pages=2)
        for e in entries:
            self.assertTrue(e["url"].startswith("https://www.degewo.de/immosuche/details/"))
            self.assertEqual(e["id"], listing_id(e["url"]))

    def test_price_is_warm_rent(self):
        entries, _ = self._crawl(max_pages=1)
        first = entries[0]
        self.assertEqual(first["price"], "360.91 €")
        self.assertEqual(first["price_warm"], first["price"])
        self.assertEqual(first["price_cold"], "")

    def test_warns_when_unique_below_result_count(self):
        with self.assertLogs("flathunt", level="WARNING") as cm:
            entries, history = self._crawl()
        self.assertEqual(history[-1].url, WARM_PAGE3_URL)  # stopped: nothing new
        self.assertEqual(len(entries), 20)
        self.assertTrue(any("collected 20 unique listings" in line and "93 Ergebnisse" in line
                            for line in cm.output))

    def test_no_shortfall_warning_when_cut_by_max_pages(self):
        with self.assertNoLogs("flathunt", level="WARNING"):
            self._crawl(max_pages=2)

    def test_stale_search_state_cookie_not_sent(self):
        # degewo restores the last search (sort + page) from this cookie on
        # every /immosuche GET; a crawl must start without it.
        SESSION.cookies.set("tx_openimmo_session", "stale", domain="www.degewo.de")
        _, history = self._crawl(max_pages=1)
        self.assertEqual([r.url for r in history], [SEARCH_URL, WARM_SORT_URL])
        self.assertNotIn("Cookie", history[0].headers)


if __name__ == "__main__":
    unittest.main()
