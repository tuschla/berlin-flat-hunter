"""degewo crawler — server-rendered HTML scrape of the Immosuche listing pages.

degewo's Immosuche lives at ``https://www.degewo.de/immosuche/``. Listings are
server-rendered as ``.c-teaser--apartment`` cards, each carrying a definition
list (Warmmiete, Zimmer, m², "frei ab" date), a title link, an image and a
stable bookmark uid (``data-openimmo-bookmark-item-uid``). There is a JSON API
(``data-openimmo-json-api``) but it only serves the bookmark widget, so we
scrape the HTML.

Pagination links carry a server-signed ``cHash`` token, so we follow the real
"next" hrefs found in the page rather than constructing them. The loop stops
early once a page yields no new cards or there's no "next" href; MAX_PAGES is
only a safety bound.

degewo advertises the Warmmiete (all-inclusive rent), so we surface that as both
``price`` and ``price_warm`` and leave ``price_cold`` empty.
"""
import re
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

from flathunter.abstract_crawler import Crawler
from flathunter.logging import logger

BASE_URL = "https://www.degewo.de"

# degewo paginates ~10 listings/page and currently lists ~75. The loop stops
# early once a page yields no new cards or there's no "next" href, so this is
# just a safe upper bound to avoid an unbounded follow-the-href loop.
MAX_PAGES = 12

# Module-level Session: connection pooling + keep-alive across crawls.
_SESSION = requests.Session()

_PRICE_RE = re.compile(r"(\d{1,3}(?:[.\s]\d{3})*(?:,\d+)?|\d+(?:,\d+)?)")
_NUM_RE = re.compile(r"(\d+(?:[.,]\d+)?)")


def _absolute_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    return urljoin(BASE_URL, href)


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


def _format_amount(value, suffix: str) -> str:
    """Format a number with a unit suffix. Returns "" when the value is
    missing/zero so schema_monitor can flag it (mirrors howoge)."""
    if value is None:
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return ""
    if num == 0:
        return ""
    if num.is_integer():
        return f"{int(num)} {suffix}"
    return f"{num:.2f} {suffix}"


class Degewo(Crawler):

    URL_PATTERN = re.compile(r"https://www\.degewo\.de")

    def get_results(self, search_url: str, max_pages: Optional[int] = None) -> list:
        entries: list = []
        seen: set = set()
        url: Optional[str] = search_url or f"{BASE_URL}/immosuche/"
        bound = MAX_PAGES if max_pages is None else min(max_pages, MAX_PAGES)

        for page in range(1, bound + 1):
            if not url:
                break
            soup = self._fetch_page(url)
            if soup is None:
                break

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
                if entry is None:
                    continue
                key = entry["id"]
                if key in seen:
                    continue
                seen.add(key)
                entries.append(entry)
                new_on_page += 1

            if new_on_page == 0:
                break
            url = self._next_page_url(soup, page + 1)

        return entries

    def _fetch_page(self, url: str) -> Optional[BeautifulSoup]:
        """GET ``url`` with one retry on 5xx / transient network failure.
        Returns a parsed soup, or None on hard failure / non-200 so the crawl
        reads as a clean empty cycle rather than raising."""
        for attempt in range(2):
            try:
                resp = _SESSION.get(url, headers=self.HEADERS, timeout=30)
            except requests.exceptions.RequestException as exc:
                if attempt == 0:
                    logger.debug("Degewo: transient GET error, retrying: %s", exc)
                    continue
                logger.warning("Degewo: could not fetch %s: %s", url, exc)
                return None
            if 500 <= resp.status_code < 600 and attempt == 0:
                continue  # one retry for transient server errors
            if resp.status_code != 200:
                logger.warning("Degewo: %s returned HTTP %d", url, resp.status_code)
                return None
            return BeautifulSoup(resp.content, "lxml")
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
        url = _absolute_url(link["href"])
        title = link.get_text(" ", strip=True)

        # Prefer the stable bookmark uid; fall back to the URL slug.
        bm = card.select_one("[data-openimmo-bookmark-item-uid]")
        uid = bm.get("data-openimmo-bookmark-item-uid") if bm else None
        if uid:
            raw_id = str(uid)
        else:
            raw_id = re.sub(r"[^A-Za-z0-9_-]", "", url.rstrip("/").rsplit("/", 1)[-1])
        try:
            entry_id: object = int(raw_id)
        except (TypeError, ValueError):
            entry_id = raw_id

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
        image = _absolute_url(img["src"]) if img and img.get("src") else ""

        price = _format_amount(price_val, "€")
        return {
            "id": entry_id,
            "url": url,
            "image": image,
            "title": title,
            "address": address,
            "rooms": _format_amount(rooms_val, "Zimmer"),
            "size": _format_amount(size_val, "m²"),
            "price": price,
            "price_cold": "",
            "price_warm": price,
            "crawler": self.get_name(),
        }

    def _next_page_url(self, soup: BeautifulSoup, next_page: int) -> Optional[str]:
        # Pagination hrefs encode the target page as "page]=N" (URL-encoded
        # "page%5D=N") and carry a server ``cHash`` token, so we follow the real
        # signed href verbatim rather than constructing it.
        needle = f"page%5D={next_page}"
        alt = f"page]={next_page}"
        for a in soup.select(
            "a[href*='paginate'], .c-pagination a[href], a[href*='page']"
        ):
            href = a.get("href", "")
            if needle in href or alt in href:
                return _absolute_url(href.split("#")[0])
        return None

    def extract_data(self, raw_data: BeautifulSoup) -> list:
        # degewo listings are driven directly via ``requests`` in get_results,
        # not through the base-class HTML soup path. Kept as a no-op for API
        # compatibility with the abstract Crawler (mirrors howoge).
        return []
