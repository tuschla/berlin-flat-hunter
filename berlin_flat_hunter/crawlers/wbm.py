"""WBM crawler — extends flathunter's Crawler base"""
import re
from typing import Optional

from bs4 import BeautifulSoup, Tag

from flathunter.abstract_crawler import Crawler

BASE_URL = "https://www.wbm.de"


class Wbm(Crawler):

    URL_PATTERN = re.compile(r"https://www\.wbm\.de")

    def get_page(self, search_url, driver=None, page_no=None) -> BeautifulSoup:
        url = search_url
        if page_no and page_no > 1:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}page={page_no}"
        return self.get_soup_from_url(url, driver)

    def get_results(self, search_url: str, max_pages: Optional[int] = None) -> list:
        entries: list = []
        page_no = 1
        while True:
            soup = self.get_page(search_url, page_no=page_no)
            page_entries = self.extract_data(soup)
            if not page_entries:
                break
            entries.extend(page_entries)
            if max_pages is not None and page_no >= max_pages:
                break
            if not self._has_next(soup, page_no):
                break
            page_no += 1
        return entries

    def extract_data(self, raw_data: BeautifulSoup) -> list:
        entries = []
        if not isinstance(raw_data, Tag):
            return entries
        for item in raw_data.find_all("div", class_="openimmo-search-list-item"):
            if isinstance(item, Tag):
                entry = self._parse_item(item)
                if entry:
                    entries.append(entry)
        return entries

    @staticmethod
    def _text(parent: Tag, cls: str) -> str:
        el = parent.find(class_=cls)
        return el.get_text(strip=True) if el else ""

    @staticmethod
    def _str_attr(tag: Tag, name: str) -> str:
        """Safely return a string attribute (BeautifulSoup may return list for multi-value attrs)."""
        value = tag.get(name, "")
        if isinstance(value, list):
            return value[0] if value else ""
        return value if isinstance(value, str) else ""

    @classmethod
    def _absolute_url(cls, href: str) -> str:
        if not href:
            return ""
        if href.startswith("http"):
            return href
        # Protocol-relative URL (//cdn/...) — keep host but use https.
        if href.startswith("//"):
            return f"https:{href}"
        return BASE_URL + href if href.startswith("/") else f"{BASE_URL}/{href}"

    def _parse_item(self, item: Tag) -> dict | None:
        uid_str = self._str_attr(item, "data-uid")
        if not uid_str:
            return None
        try:
            uid = int(uid_str)
        except ValueError:
            return None

        title = self._text(item, "imageTitle")
        if not title:
            return None

        anchor = item.find("a", class_="immo-button-cta") or item.find("a", href=True)
        if not anchor or not isinstance(anchor, Tag):
            return None
        href = self._str_attr(anchor, "href")
        if not href:
            return None

        img_wrap = item.find(class_="imgWrap")
        img_src = self._str_attr(img_wrap, "data-img-src") if isinstance(img_wrap, Tag) else ""

        return {
            "id": uid,
            "url": self._absolute_url(href),
            "image": self._absolute_url(img_src) if img_src else "",
            "title": title,
            "address": self._text(item, "address"),
            "rooms": self._text(item, "main-property-rooms"),
            "size": self._text(item, "main-property-size"),
            "price": self._text(item, "main-property-rent"),
            "crawler": self.get_name(),
        }

    def _has_next(self, soup: BeautifulSoup, current_page: int) -> bool:
        pagination = soup.find("ul", class_="pagination")
        if not pagination or not isinstance(pagination, Tag):
            return False
        # Match page number with end-of-value or non-digit boundary so page=2 doesn't match page=20.
        pattern = re.compile(rf"[?&]page={current_page + 1}(?:&|$|#)")
        for link in pagination.find_all("a"):
            if isinstance(link, Tag) and pattern.search(self._str_attr(link, "href")):
                return True
        return False
