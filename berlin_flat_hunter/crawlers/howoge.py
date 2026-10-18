"""Howoge crawler — TYPO3 RealEstate JSON endpoint"""
import re
from typing import Optional

from bs4 import BeautifulSoup

from flathunter.abstract_crawler import Crawler
from flathunter.logging import logger

from berlin_flat_hunter.crawlers.common import (
    absolute_url,
    format_amount,
    listing_id,
    post_with_retry,
)

BASE_URL = "https://www.howoge.de"

# TYPO3 ext:HowRealestate exposes a JSON list endpoint that the standard
# search page (`/immobiliensuche/wohnungssuche.html`) drives via AJAX. The
# endpoint accepts a POST with paging params and returns the same items the
# rendered list shows — without us having to scrape an HTML SPA.
JSON_ENDPOINT = f"{BASE_URL}/?type=999&tx_howrealestate_json_list%5Baction%5D=immoList"

_PAGE_SIZE = 50

# Hard ceiling on pages per crawl when the caller passes no ``max_pages``
# (the orchestrator never does). 20 × 50 = 1000 listings, ~20× Howoge's
# typical stock; hitting it means pagination is broken, not that it's busy.
_MAX_PAGES = 20


class Howoge(Crawler):

    URL_PATTERN = re.compile(r"https://www\.howoge\.de")

    def get_results(self, search_url: str, max_pages: Optional[int] = None) -> list:
        entries: list = []
        seen: set = set()
        page_cap = max_pages if max_pages is not None else _MAX_PAGES
        page = 1
        while True:
            batch = self._fetch_page(page)
            if not batch:
                break
            new = 0
            for item in batch:
                entry = self._parse_item(item)
                if entry is None or entry["id"] in seen:
                    continue
                seen.add(entry["id"])
                new += 1
                entries.append(entry)
            # The live endpoint ignores the page/limit params and returns the
            # full list for every page. A page with nothing new means we've
            # seen everything; without this a list of ≥ _PAGE_SIZE items is
            # re-fetched forever (each page looks "full").
            if not new:
                break
            if len(batch) < _PAGE_SIZE:
                break
            if page >= page_cap:
                if max_pages is None:
                    logger.warning("Howoge: stopped at the %d-page cap with every page "
                                   "still returning new listings — pagination may be broken",
                                   _MAX_PAGES)
                break
            page += 1
        return entries

    def _fetch_page(self, page: int) -> list:
        data = {
            "tx_howrealestate_json_list[page]": str(page),
            "tx_howrealestate_json_list[limit]": str(_PAGE_SIZE),
        }
        resp = post_with_retry(JSON_ENDPOINT, label="Howoge", headers=self.HEADERS,
                               timeout=20, data=data)
        if resp is None or resp.status_code != 200:
            if resp is not None:
                logger.warning("Howoge: HTTP %d for page %d", resp.status_code, page)
            return []
        try:
            payload = resp.json()
        except ValueError as exc:
            logger.warning("Howoge: invalid JSON for page %d: %s", page, exc)
            return []
        items = payload.get("immoobjects")
        return items if isinstance(items, list) else []

    def _parse_item(self, item: dict) -> Optional[dict]:
        link = item.get("link", "")
        if not link:
            return None
        url = absolute_url(BASE_URL, link)

        # title field is the postal address ("Streitstraße 5, 13587 Berlin");
        # notice field is the descriptive label ("3-Zimmer-Wohnung (WBS 100-140)").
        # Mapping: address ← title, title ← notice (fallback to a synthesized
        # label) so plz_filter can grep the PLZ out of address as on other sites.
        address = (item.get("title") or "").strip()
        notice = (item.get("notice") or "").strip()
        district = (item.get("district") or "").strip()
        title = notice or self._synthesize_title(item, district)

        # The JSON ``rent`` is the Warmmiete (the detail page's "Warmmiete").
        price = format_amount(item.get("rent"), "€")
        return {
            "id": listing_id(url),
            "url": url,
            "image": absolute_url(BASE_URL, item.get("image", "")),
            "title": title,
            "address": f"{address} ({district})" if district and address else address,
            "rooms": format_amount(item.get("rooms"), "Zimmer"),
            "size": format_amount(item.get("area"), "m²"),
            "price": price,
            "price_cold": "",
            "price_warm": price,
            "crawler": self.get_name(),
        }

    @staticmethod
    def _synthesize_title(item: dict, district: str) -> str:
        rooms = item.get("rooms")
        parts = []
        if rooms:
            parts.append(f"{rooms}-Zimmer-Wohnung")
        else:
            parts.append("Wohnung")
        if district:
            parts.append(f"in {district}")
        return " ".join(parts)

    def extract_data(self, raw_data: BeautifulSoup) -> list:
        # Howoge listings come from the JSON endpoint, not from HTML — the
        # base-class HTML soup path is not used. Kept as a no-op for API
        # compatibility with the abstract Crawler.
        return []
