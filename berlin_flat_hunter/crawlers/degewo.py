"""degewo crawler — server-rendered HTML scrape of the Immosuche listing pages.

degewo's Immosuche lives at ``https://www.degewo.de/immosuche/``. Listings are
server-rendered as ``.c-teaser--apartment`` cards, each carrying a definition
list (Warmmiete, Zimmer, m², "frei ab" date), a title link and an image. There
is a JSON API (``data-openimmo-json-api``) but it only serves the bookmark
widget, so we scrape the HTML. The listing id is derived from the detail URL
(``listing_id``); the bookmark uid is not stable.

The site's default sort ("Anzahl Zimmer") ties listings, so its pages overlap
and a crawl of all of them sees only ~2/3 of the stock. Page 1 offers its own
signed "Warmmiete" sort href (``…sortBy%5D=immobilie_preise_warmmiete&cHash=…``);
we read it from the page and restart the crawl from it, which yields a stable
order. Pagination links carry a server-signed ``cHash`` token too, so we follow
the real "next" hrefs found in the page rather than constructing them. The loop
stops once a page yields no new cards or there's no "next" href; MAX_PAGES is
only a safety bound. Page 1's "N Ergebnisse" count is compared with the unique
listings collected, and a shortfall is logged as a warning.

degewo keeps the last search (sort + page) in a year-long ``tx_openimmo_session``
cookie and restores it on every ``/immosuche`` GET — even the bare search URL —
so each crawl drops the site's cookies first and starts from page 1.

degewo advertises the Warmmiete (all-inclusive rent), so we surface that as both
``price`` and ``price_warm`` and leave ``price_cold`` empty.
"""
import re
from typing import Optional
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from flathunter.abstract_crawler import Crawler
from flathunter.logging import logger

from berlin_flat_hunter.crawlers.common import (
    SESSION,
    absolute_url,
    format_amount,
    get_with_retry,
    listing_id,
)

BASE_URL = "https://www.degewo.de"

# degewo paginates 10 listings/page and currently lists ~93. The loop stops
# once a page yields no new cards or there's no "next" href, so this is just a
# safe upper bound (200 listings) on the follow-the-href loop.
MAX_PAGES = 20

_PRICE_RE = re.compile(r"(\d{1,3}(?:[.\s]\d{3})*(?:,\d+)?|\d+(?:,\d+)?)")
_NUM_RE = re.compile(r"(\d+(?:[.,]\d+)?)")
_RESULT_COUNT_RE = re.compile(r"(\d[\d.]*)\s*Ergebnis")
_WARM_SORT_RE = re.compile(r"sortBy(?:%5D|\])=immobilie_preise_warmmiete")


def _parse_amount(text: Optional[str]) -> Optional[float]:
    """Pull the first German-formatted number out of ``text`` (e.g. "812,00 €")."""
    if not text:
        return None
    m = _PRICE_RE.search(text)
    if not m:
        return None
    raw = m.group(1).replace(".", "").replace(" ", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


class Degewo(Crawler):

    URL_PATTERN = re.compile(r"https://www\.degewo\.de")

    def get_results(self, search_url: str, max_pages: Optional[int] = None) -> list:
        self._forget_search_state()
        bound = MAX_PAGES if max_pages is None else min(max_pages, MAX_PAGES)

        soup = self._fetch_page(search_url or f"{BASE_URL}/immosuche/")
        if soup is None:
            return []
        expected = self._result_count(soup)
        sorted_url = self._warm_rent_sort_url(soup)
        if sorted_url:
            soup = self._fetch_page(sorted_url)
        else:
            logger.warning("Degewo: no Warmmiete sort option on page 1 — crawling the "
                           "default sort, whose pages overlap")

        entries: list = []
        seen: set = set()
        cut_by_max_pages = False
        page = 1
        while soup is not None:
            cards = soup.select(
                ".c-teaser--apartment, article.c-teaser--apartment, "
                "article.search-result, .article-list__item"
            )
            if not cards:
                logger.warning("Degewo: no cards on page %d (markup change or block?)", page)
                break

            new_on_page = 0
            for card in cards:
                try:
                    entry = self._parse_card(card)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("Degewo: card parse failed: %s", exc)
                    continue
                if entry is None or entry["id"] in seen:
                    continue
                seen.add(entry["id"])
                entries.append(entry)
                new_on_page += 1

            if new_on_page == 0:
                break
            next_url = self._next_page_url(soup, page + 1)
            if not next_url:
                break
            if page >= bound:
                if max_pages is not None and page >= max_pages:
                    cut_by_max_pages = True
                else:
                    logger.warning("Degewo: stopped at the %d-page cap with a next page "
                                   "still linked", MAX_PAGES)
                break
            page += 1
            soup = self._fetch_page(next_url)

        if expected is not None and len(entries) < expected and not cut_by_max_pages:
            logger.warning("Degewo: collected %d unique listings but the site reports "
                           "%d Ergebnisse", len(entries), expected)
        return entries

    @staticmethod
    def _forget_search_state() -> None:
        """Drop degewo's cookies so its stored search (sort + page) can't be
        restored into this crawl's first GET."""
        try:
            SESSION.cookies.clear(domain=urlsplit(BASE_URL).hostname)
        except KeyError:
            pass  # no cookies stored for the site yet

    def _fetch_page(self, url: str) -> Optional[BeautifulSoup]:
        """Parsed soup, or None on hard failure / non-200 so the crawl reads as
        a clean empty cycle rather than raising."""
        resp = get_with_retry(url, label="Degewo", headers=self.HEADERS, timeout=30)
        if resp is None:
            return None
        if resp.status_code != 200:
            logger.warning("Degewo: %s returned HTTP %d", url, resp.status_code)
            return None
        return BeautifulSoup(resp.content, "lxml")

    @staticmethod
    def _result_count(soup: BeautifulSoup) -> Optional[int]:
        """The page's "N Ergebnisse" total, or None when it isn't shown."""
        el = soup.select_one(".results-count")
        if el is None:
            return None
        m = _RESULT_COUNT_RE.search(el.get_text(" ", strip=True))
        return int(m.group(1).replace(".", "")) if m else None

    @staticmethod
    def _warm_rent_sort_url(soup: BeautifulSoup) -> Optional[str]:
        """The page's own signed "sort by Warmmiete" href (a ``<select>``
        option value), or None when the page doesn't offer it."""
        for el in soup.select("option[value], a[href]"):
            href = el.get("value") if el.name == "option" else el.get("href")
            if isinstance(href, str) and _WARM_SORT_RE.search(href):
                return absolute_url(BASE_URL, href.split("#")[0])
        return None

    def _parse_card(self, card: Tag) -> Optional[dict]:
        # Try selectors in priority order: a CSS selector *group* matches the
        # first element in document order (which is the image anchor, with no
        # text), so we must probe the headline link explicitly first.
        link = None
        for selector in ("h3 a.c-headline--linked", "h3 a", ".c-headline--linked",
                         "a[href*='/details/']"):
            candidate = card.select_one(selector)
            if candidate and candidate.get("href"):
                link = candidate
                break
        if not link or not link.get("href"):
            link = card.find("a", href=True)
        if not link or not link.get("href"):
            return None
        url = absolute_url(BASE_URL, link["href"])
        title = link.get_text(" ", strip=True)

        addr_el = card.select_one(".c-copy > p, p")
        address = addr_el.get_text(" ", strip=True) if addr_el else ""

        price_val = size_val = rooms_val = None
        for item in card.select(".c-definition-list__item"):
            term = item.select_one(".c-definition-list__term")
            defn = item.select_one(".c-definition-list__definition")
            if not term or not defn:
                continue
            value = term.get_text(" ", strip=True)
            label = defn.get_text(" ", strip=True).lower()
            if "miete" in label:
                price_val = _parse_amount(value)
            elif "zimmer" in label:
                m = _NUM_RE.search(value)
                rooms_val = float(m.group(1).replace(",", ".")) if m else None
            elif "m²" in label or "m2" in label or "fläche" in label:
                m = _NUM_RE.search(value)
                size_val = float(m.group(1).replace(",", ".")) if m else None

        img = card.select_one("img.c-img[src], img[src]")
        image = absolute_url(BASE_URL, img["src"]) if img and img.get("src") else ""

        price = format_amount(price_val, "€")
        return {
            "id": listing_id(url),
            "url": url,
            "image": image,
            "title": title,
            "address": address,
            "rooms": format_amount(rooms_val, "Zimmer"),
            "size": format_amount(size_val, "m²"),
            "price": price,
            "price_cold": "",
            "price_warm": price,
            "crawler": self.get_name(),
        }

    def _next_page_url(self, soup: BeautifulSoup, next_page: int) -> Optional[str]:
        # Pagination hrefs encode the target page as "page]=N" (URL-encoded
        # "page%5D=N") and carry a server ``cHash`` token, so we follow the real
        # signed href verbatim rather than constructing it. The digit lookahead
        # keeps page 2 from matching page 20.
        pattern = re.compile(rf"page(?:%5D|\])={next_page}(?!\d)")
        for a in soup.select(
            "a[href*='paginate'], .c-pagination a[href], a[href*='page']"
        ):
            href = a.get("href", "")
            if pattern.search(href):
                return absolute_url(BASE_URL, href.split("#")[0])
        return None

    def extract_data(self, raw_data: BeautifulSoup) -> list:
        # degewo listings are driven directly via ``requests`` in get_results,
        # not through the base-class HTML soup path. Kept as a no-op for API
        # compatibility with the abstract Crawler (mirrors howoge).
        return []
