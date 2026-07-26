"""Tests for the degewo crawler — requests-mocked HTML scrape."""
import unittest

import requests_mock as req_mock

from berlin_flat_hunter.crawlers.degewo import Degewo

SEARCH_URL = "https://www.degewo.de/immosuche/"


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
        first = next(e for e in entries if e["id"] == 100123)
        self.assertEqual(first["url"],
                         "https://www.degewo.de/immosuche/details/wohnung-eins/")
        self.assertEqual(first["title"], "3-Zimmer-Wohnung in Marzahn")
        self.assertEqual(first["price"], "812 €")
        self.assertEqual(first["price_warm"], "812 €")
        self.assertEqual(first["price_cold"], "")
        self.assertEqual(first["rooms"], "3 Zimmer")
        self.assertEqual(first["size"], "72.50 m²")
        self.assertEqual(first["address"], "Musterstraße 1, 12679 Berlin")
        self.assertEqual(first["crawler"], "Degewo")

    def test_id_is_int_from_bookmark_uid(self):
        entries = self._get(FIXTURE)
        self.assertTrue(all(isinstance(e["id"], int) for e in entries))

    def test_image_absolutised(self):
        entries = self._get(FIXTURE)
        first = next(e for e in entries if e["id"] == 100123)
        second = next(e for e in entries if e["id"] == 100456)
        self.assertEqual(first["image"],
                         "https://www.degewo.de/fileadmin/img/eins.jpg")
        self.assertEqual(second["image"],
                         "https://www.degewo.de/fileadmin/img/zwei.jpg")

    def test_integer_size_formats_without_decimals(self):
        entries = self._get(FIXTURE)
        second = next(e for e in entries if e["id"] == 100456)
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


if __name__ == "__main__":
    unittest.main()
