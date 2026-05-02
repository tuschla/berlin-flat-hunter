"""Howoge crawler — TYPO3 RealEstate JSON endpoint"""
import re
from typing import Optional

import requests
from bs4 import BeautifulSoup

from flathunter.abstract_crawler import Crawler
from flathunter.logging import logger

BASE_URL = "https://www.howoge.de"

# TYPO3 ext:HowRealestate exposes a JSON list endpoint that the standard
# search page (`/immobiliensuche/wohnungssuche.html`) drives via AJAX. The
# endpoint accepts a POST with paging params and returns the same items the
# rendered list shows — without us having to scrape an HTML SPA.
JSON_ENDPOINT = f"{BASE_URL}/?type=999&tx_howrealestate_json_list%5Baction%5D=immoList"

_PAGE_SIZE = 50

_SESSION = requests.Session()


def _absolute_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    return BASE_URL + href if href.startswith("/") else f"{BASE_URL}/{href}"


def _format_int_amount(value, suffix: str) -> str:
    """JSON returns rent/area/rooms as numbers; format with a unit suffix.
    Returns "" when the value is missing/zero so schema_monitor can flag it."""
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


class Howoge(Crawler):

    URL_PATTERN = re.compile(r"https://www\.howoge\.de")

    def get_results(self, search_url: str, max_pages: Optional[int] = None) -> list:
        entries: list = []
        page = 1
        while True:
            batch = self._fetch_page(page)
            if not batch:
                break
            for item in batch:
                entry = self._parse_item(item)
                if entry:
                    entries.append(entry)
            if max_pages is not None and page >= max_pages:
                break
            if len(batch) < _PAGE_SIZE:
                break
            page += 1
        return entries

    def _fetch_page(self, page: int) -> list:
        data = {
            "tx_howrealestate_json_list[page]": str(page),
            "tx_howrealestate_json_list[limit]": str(_PAGE_SIZE),
        }
        try:
            resp = _SESSION.post(JSON_ENDPOINT, data=data, headers=self.HEADERS, timeout=20)
        except requests.exceptions.RequestException as exc:
            logger.warning("Howoge: request failed for page %d: %s", page, exc)
            return []
        if resp.status_code != 200:
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
        uid = item.get("uid")
        link = item.get("link", "")
        if uid is None or not link:
            return None
        try:
            uid_int = int(uid)
        except (TypeError, ValueError):
            return None

        # title field is the postal address ("Streitstraße 5, 13587 Berlin");
        # notice field is the descriptive label ("3-Zimmer-Wohnung (WBS 100-140)").
        # Mapping: address ← title, title ← notice (fallback to a synthesized
        # label) so plz_filter can grep the PLZ out of address as on other sites.
        address = (item.get("title") or "").strip()
        notice = (item.get("notice") or "").strip()
        district = (item.get("district") or "").strip()
        title = notice or self._synthesize_title(item, district)

        return {
            "id": uid_int,
            "url": _absolute_url(link),
            "image": _absolute_url(item.get("image", "")),
            "title": title,
            "address": f"{address} ({district})" if district and address else address,
            "rooms": _format_int_amount(item.get("rooms"), "Zimmer"),
            "size": _format_int_amount(item.get("area"), "m²"),
            "price": _format_int_amount(item.get("rent"), "€"),
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
