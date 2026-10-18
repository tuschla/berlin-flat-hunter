"""WBM crawler — extends flathunter's Crawler base"""
import re
from typing import Optional

from bs4 import BeautifulSoup, Tag

from flathunter.abstract_crawler import Crawler
from flathunter.logging import logger

from berlin_flat_hunter.crawlers.common import (
    absolute_url,
    amount_or_empty,
    listing_id,
    str_attr,
)

BASE_URL = "https://www.wbm.de"


class Wbm(Crawler):

    URL_PATTERN = re.compile(r"https://www\.wbm\.de")

    # Set to True by get_results() when the WBM listings page rendered its
    # intentional "no available listings today" state — distinguishes a
    # legitimate empty result from a scraping failure so the schema monitor
    # doesn't cry wolf. Consumed by BerlinHunter._record_health.
    last_crawl_site_reported_empty: bool = False

    def get_page(self, search_url, driver=None, page_no=None) -> BeautifulSoup:
        url = search_url
        if page_no and page_no > 1:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}page={page_no}"
        return self.get_soup_from_url(url, driver)

    def get_results(self, search_url: str, max_pages: Optional[int] = None) -> list:
        entries: list = []
        seen: set = set()
        self.last_crawl_site_reported_empty = False
        page_no = 1
        while True:
            soup = self.get_page(search_url, page_no=page_no)
            if page_no == 1 and self._is_site_reported_empty(soup):
                logger.info("WBM: site reports no available listings "
                            "(intentional empty state, not a scrape failure)")
                self.last_crawl_site_reported_empty = True
                return []
            page_entries = self.extract_data(soup)
            if not page_entries:
                break
            for entry in page_entries:
                if entry["id"] not in seen:
                    seen.add(entry["id"])
                    entries.append(entry)
            if max_pages is not None and page_no >= max_pages:
                break
            if not self._has_next(soup, page_no):
                break
            page_no += 1
        return entries

    @staticmethod
    def _is_site_reported_empty(soup: BeautifulSoup) -> bool:
        """WBM's TYPO3 openimmo container tags itself ``empty`` and renders a
        ``.openimmo-no-offer-avilable`` (sic) block when no apartments are
        currently listed. Either marker is a reliable signal that the page
        rendered correctly with zero results."""
        if not isinstance(soup, Tag):
            return False
        if soup.select_one(".openimmo-no-offer-avilable"):
            return True
        container = soup.select_one("div.tx-openimmo.search")
        if container is not None:
            classes = container.get("class") or []
            if isinstance(classes, str):
                classes = classes.split()
            if "empty" in classes:
                return True
        return False

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

    def _parse_item(self, item: Tag) -> dict | None:
        # WBM's ``data-uid`` changes for the same listing every day or two, so it
        # is only kept as the "this is a real listing card" marker; the id is
        # derived from the detail URL.
        if not str_attr(item, "data-uid"):
            return None
        title = self._text(item, "imageTitle")
        if not title:
            return None

        anchor = item.find("a", class_="immo-button-cta") or item.find("a", href=True)
        if not anchor or not isinstance(anchor, Tag):
            return None
        url = absolute_url(BASE_URL, str_attr(anchor, "href"))
        if not url:
            return None

        img_wrap = item.find(class_="imgWrap")
        img_src = str_attr(img_wrap, "data-img-src") if isinstance(img_wrap, Tag) else ""

        # main-property-rent is labelled "Warmmiete" on the card.
        price = amount_or_empty(self._text(item, "main-property-rent"))
        return {
            "id": listing_id(url),
            "url": url,
            "image": absolute_url(BASE_URL, img_src),
            "title": title,
            "address": self._text(item, "address"),
            "rooms": self._text(item, "main-property-rooms"),
            "size": self._text(item, "main-property-size"),
            "price": price,
            "price_cold": "",
            "price_warm": price,
            "crawler": self.get_name(),
        }

    def _has_next(self, soup: BeautifulSoup, current_page: int) -> bool:
        pagination = soup.find("ul", class_="pagination")
        if not pagination or not isinstance(pagination, Tag):
            return False
        # Match page number with end-of-value or non-digit boundary so page=2 doesn't match page=20.
        pattern = re.compile(rf"[?&]page={current_page + 1}(?:&|$|#)")
        for link in pagination.find_all("a"):
            if isinstance(link, Tag) and pattern.search(str_attr(link, "href")):
                return True
        return False
