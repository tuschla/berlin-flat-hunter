"""Gesobau crawler — Berlin public housing"""
import re
from typing import Optional

from bs4 import BeautifulSoup, Tag

from flathunter.abstract_crawler import Crawler

BASE_URL = "https://www.gesobau.de"

_WHITESPACE_RE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def _absolute_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return f"https:{href}"
    return BASE_URL + href if href.startswith("/") else f"{BASE_URL}/{href}"


class Gesobau(Crawler):

    URL_PATTERN = re.compile(r"https://www\.gesobau\.de")

    def get_results(self, search_url: str, max_pages: Optional[int] = None) -> list:
        # Gesobau renders all listings on a single page (no pagination as of 2026-04)
        soup = self.get_soup_from_url(search_url)
        return self.extract_data(soup)

    def extract_data(self, raw_data: BeautifulSoup) -> list:
        if not isinstance(raw_data, Tag):
            return []
        entries = []
        for item in raw_data.find_all("article", class_="apartment"):
            if isinstance(item, Tag):
                entry = self._parse_item(item)
                if entry:
                    entries.append(entry)
        return entries

    @staticmethod
    def _str_attr(tag: Tag, name: str) -> str:
        value = tag.get(name, "")
        if isinstance(value, list):
            return value[0] if value else ""
        return value if isinstance(value, str) else ""

    def _parse_item(self, item: Tag) -> Optional[dict]:
        uid_str = self._str_attr(item, "data-apartment-uid")
        if not uid_str:
            return None
        try:
            uid = int(uid_str)
        except ValueError:
            return None

        title_link = item.select_one("h3.basicTeaser__title a")
        if not title_link or not isinstance(title_link, Tag):
            return None
        title = _normalise(title_link.get_text(" ", strip=True))
        href = self._str_attr(title_link, "href")
        url = _absolute_url(href)
        if not url:
            return None

        district_el = item.select_one(".meta__region")
        district = _normalise(district_el.get_text(" ", strip=True)) if district_el else ""
        addr_el = item.select_one(".basicTeaser__text span")
        address_part = _normalise(addr_el.get_text(" ", strip=True)) if addr_el else ""
        address = f"{address_part}, {district}".strip(", ") if district else address_part

        # apartment__info contains 3 spans: price, rooms, size
        info = item.find(class_="apartment__info")
        price = rooms = size = ""
        if info and isinstance(info, Tag):
            spans = [_normalise(s.get_text(" ", strip=True)) for s in info.find_all("span")
                     if s.get_text(strip=True)]
            for s in spans:
                if "€" in s and not price:
                    price = s
                elif "Zimmer" in s and not rooms:
                    rooms = s
                elif "m²" in s and not size:
                    size = s

        img_el = item.select_one("img")
        img_src = _normalise(self._str_attr(img_el, "src")) if img_el and isinstance(img_el, Tag) else ""
        image = _absolute_url(img_src)

        return {
            "id": uid,
            "url": url,
            "image": image,
            "title": title,
            "address": address,
            "rooms": rooms,
            "size": size,
            "price": price,
            "crawler": self.get_name(),
        }
