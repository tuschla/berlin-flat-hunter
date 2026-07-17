"""Kleinanzeigen crawler — subclass that fetches the list page with plain
``requests``, repairs size/rooms extraction, and enriches exposes with the
description snippet the list page already carries.

Three problems with flathunter's upstream Kleinanzeigen crawler as of 2026-07:

1. It fetches the list page through ``WebdriverCrawler`` (undetected-
   chromedriver). Kleinanzeigen's bot detection now serves the headless-Chrome
   fingerprint a captcha/empty page — so ``srchrslt-adtable`` is absent and the
   crawl returns 0 results (and the upstream ``extract_data`` then crashes with
   ``AttributeError`` on ``None.find_all``). The list page is plain server-
   rendered HTML, though: a bare ``requests`` GET with a browser User-Agent
   returns the full results from the same IP. So we override ``get_page`` to
   use ``requests`` and skip the browser entirely — more reliable and faster,
   and it sidesteps the wedged-driver failure mode too.

2. It reads size/rooms from ``.simpletag`` elements, which Kleinanzeigen has
   removed. ``tags[0]`` now resolves to the seller-type badge ("Von Privat"),
   so ``size`` is garbage and ``rooms`` is always empty. Size + area now live
   in a single ``.aditem-main--middle--tags`` block as ``"72,13 m² · 2 Zi."``.

3. The ``.aditem-main--middle--description`` snippet is discarded, robbing the
   ScamFilter of its best signal — scam templates put "currently abroad",
   "WhatsApp", email addresses, etc. in the description body, not the title.

We reuse upstream's ``extract_data`` for the fields it still gets right
(id/url/title/price/address/image) and, in a second pass keyed by
``data-adid``, overwrite size + rooms and attach description. No extra HTTP
requests — everything ships with the list page.
"""
import re

import requests
from bs4 import BeautifulSoup, Tag

from flathunter.crawler.kleinanzeigen import Kleinanzeigen as _UpstreamKleinanzeigen
from flathunter.logging import logger

# "72,13 m²" / "28 m²" — German or plain decimal, tolerant of the trailing unit.
_SIZE_RE = re.compile(r"\d+(?:[.,]\d+)?\s*m²")
# "2 Zi." / "1,5 Zi" — room count preceding the Zimmer abbreviation.
_ROOMS_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*Zi\.?", re.IGNORECASE)

# Module-level session for connection pooling + keep-alive across crawls.
_SESSION = requests.Session()
# A real browser UA + German locale is all Kleinanzeigen's list page wants —
# it serves full server-rendered results to a plain GET from this IP.
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}


class Kleinanzeigen(_UpstreamKleinanzeigen):
    """Kleinanzeigen crawler: requests-based list fetch, fixed size/rooms, description."""

    def get_page(self, search_url: str, driver=None, page_no=None) -> BeautifulSoup:
        """Fetch the list page with plain ``requests`` (no webdriver).

        One retry on transient failure. On hard failure returns an empty soup so
        ``extract_data`` degrades to ``[]`` rather than raising — the crawl reads
        as a clean empty cycle and the schema monitor / watchdog take over.
        """
        for attempt in range(2):
            try:
                resp = _SESSION.get(search_url, headers=_HEADERS, timeout=30)
            except requests.exceptions.RequestException as exc:
                if attempt == 0:
                    continue
                logger.warning("Kleinanzeigen: list fetch failed for %s: %s", search_url, exc)
                return BeautifulSoup("", "lxml")
            if 500 <= resp.status_code < 600 and attempt == 0:
                continue
            if resp.status_code != 200:
                logger.warning("Kleinanzeigen: list page %s returned HTTP %d",
                               search_url, resp.status_code)
                return BeautifulSoup("", "lxml")
            return BeautifulSoup(resp.content, "lxml")
        return BeautifulSoup("", "lxml")

    def extract_data(self, raw_data: BeautifulSoup) -> list:
        # Guard: upstream does ``raw_data.find(id="srchrslt-adtable").find_all(...)``
        # and crashes if the container is absent (block/captcha/empty page).
        # Degrade to [] and log instead — a clean empty crawl, not a traceback.
        if raw_data is None or raw_data.find(id="srchrslt-adtable") is None:
            logger.warning("Kleinanzeigen: results container absent — "
                           "blocked, captcha, or genuinely empty page")
            return []
        entries = super().extract_data(raw_data)
        if not entries:
            return entries
        extras = self._extras_by_id(raw_data)
        for entry in entries:
            extra = extras.get(entry.get("id"))
            if not extra:
                continue
            if extra.get("size"):
                entry["size"] = extra["size"]
            if extra.get("rooms"):
                entry["rooms"] = extra["rooms"]
            if extra.get("description"):
                entry["description"] = extra["description"]
        return entries

    @classmethod
    def _extras_by_id(cls, raw_data: BeautifulSoup) -> dict[int, dict]:
        by_id: dict[int, dict] = {}
        table = raw_data.find(id="srchrslt-adtable") if isinstance(raw_data, Tag) else None
        if not isinstance(table, Tag):
            return by_id
        for article in table.find_all("article", class_="aditem"):
            if not isinstance(article, Tag):
                continue
            adid_raw = article.get("data-adid")
            if not adid_raw:
                continue
            try:
                adid = int(adid_raw if isinstance(adid_raw, str) else adid_raw[0])
            except (ValueError, TypeError, IndexError):
                continue
            by_id[adid] = cls._parse_article(article)
        return by_id

    @classmethod
    def _parse_article(cls, article: Tag) -> dict:
        out: dict[str, str] = {}
        tags_el = article.find(class_="aditem-main--middle--tags")
        if isinstance(tags_el, Tag):
            text = tags_el.get_text(" ", strip=True)
            size_match = _SIZE_RE.search(text)
            if size_match:
                # Collapse internal whitespace: "72,13  m²" -> "72,13 m²".
                out["size"] = " ".join(size_match.group().split())
            rooms_match = _ROOMS_RE.search(text)
            if rooms_match:
                out["rooms"] = rooms_match.group(1)
        desc_el = article.find(class_="aditem-main--middle--description")
        if isinstance(desc_el, Tag):
            desc = desc_el.get_text(" ", strip=True)
            if desc:
                out["description"] = desc
        return out
