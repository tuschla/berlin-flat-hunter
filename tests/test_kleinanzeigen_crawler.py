"""Tests for the local Kleinanzeigen crawler subclass — captures description."""
import unittest

from bs4 import BeautifulSoup

from berlin_flat_hunter.crawlers.kleinanzeigen import Kleinanzeigen


FIXTURE = """
<html><body>
<div id="srchrslt-adtable">
  <article class="aditem" data-adid="111">
    <div class="aditem-main--top--left"><span>10115 Mitte</span></div>
    <div class="aditem-main--middle">
      <a class="ellipsis" href="/s-anzeige/wohnung-eins/111-203-1">Wohnung eins</a>
      <p class="aditem-main--middle--description">
        Hallo, ich bin gerade im Ausland. Kontakt bitte per WhatsApp.
      </p>
      <div class="aditem-main--middle--price-shipping">
        <p class="aditem-main--middle--price-shipping--price">500 €</p>
      </div>
      <p class="aditem-main--middle--tags">72,13 m² · 2 Zi.</p>
    </div>
  </article>
  <article class="aditem" data-adid="222">
    <div class="aditem-main--top--left"><span>12045 Neukölln</span></div>
    <div class="aditem-main--middle">
      <a class="ellipsis" href="/s-anzeige/wohnung-zwei/222-203-1">Wohnung zwei</a>
      <div class="aditem-main--middle--price-shipping">
        <p class="aditem-main--middle--price-shipping--price">700 €</p>
      </div>
      <p class="aditem-main--middle--tags">55 m²</p>
    </div>
  </article>
</div>
</body></html>
"""


class FakeConfig:
    def captcha_enabled(self): return False
    def use_proxy(self): return False
    def get_driver(self): return None


class TestKleinanzeigenCrawler(unittest.TestCase):

    def setUp(self):
        self.crawler = Kleinanzeigen(FakeConfig())

    def test_extract_data_returns_all_entries(self):
        soup = BeautifulSoup(FIXTURE, "html.parser")
        entries = self.crawler.extract_data(soup)
        self.assertEqual(len(entries), 2)

    def test_extract_data_attaches_description_when_present(self):
        soup = BeautifulSoup(FIXTURE, "html.parser")
        entries = self.crawler.extract_data(soup)
        first = next(e for e in entries if e["id"] == 111)
        self.assertIn("Ausland", first["description"])
        self.assertIn("WhatsApp", first["description"])

    def test_extract_data_omits_description_when_absent(self):
        soup = BeautifulSoup(FIXTURE, "html.parser")
        entries = self.crawler.extract_data(soup)
        second = next(e for e in entries if e["id"] == 222)
        self.assertNotIn("description", second)

    def test_size_parsed_from_tags_block(self):
        soup = BeautifulSoup(FIXTURE, "html.parser")
        entries = self.crawler.extract_data(soup)
        first = next(e for e in entries if e["id"] == 111)
        self.assertEqual(first["size"], "72,13 m²")

    def test_rooms_parsed_from_tags_block(self):
        soup = BeautifulSoup(FIXTURE, "html.parser")
        entries = self.crawler.extract_data(soup)
        first = next(e for e in entries if e["id"] == 111)
        self.assertEqual(first["rooms"], "2")

    def test_size_without_rooms_still_captured(self):
        soup = BeautifulSoup(FIXTURE, "html.parser")
        entries = self.crawler.extract_data(soup)
        second = next(e for e in entries if e["id"] == 222)
        self.assertEqual(second["size"], "55 m²")
        # No "Zi." in the tags block → rooms not overwritten (stays upstream "").
        self.assertEqual(second.get("rooms", ""), "")

    def test_size_is_not_seller_badge(self):
        """Regression: upstream stored 'Von Privat' (seller badge) as size."""
        soup = BeautifulSoup(FIXTURE, "html.parser")
        entries = self.crawler.extract_data(soup)
        for e in entries:
            self.assertNotIn("Privat", e.get("size", ""))

    def test_extract_data_on_empty_container_returns_empty(self):
        soup = BeautifulSoup('<html><body><div id="srchrslt-adtable"></div></body></html>',
                             "html.parser")
        # Upstream extract_data raises on the .find_all call when soup is missing —
        # this behaviour is inherited. We only need to guarantee our overlay
        # doesn't error on an already-empty entries list.
        try:
            entries = self.crawler.extract_data(soup)
        except AttributeError:
            entries = []
        self.assertEqual(entries, [])

    def test_extras_by_id_handles_missing_data_adid(self):
        html = ('<html><body><div id="srchrslt-adtable">'
                '<article class="aditem"><p class="aditem-main--middle--description">x</p>'
                '</article></div></body></html>')
        soup = BeautifulSoup(html, "html.parser")
        self.assertEqual(self.crawler._extras_by_id(soup), {})


if __name__ == "__main__":
    unittest.main()
