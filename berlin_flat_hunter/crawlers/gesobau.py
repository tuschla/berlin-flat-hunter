"""Gesobau crawler — Berlin public housing.

The Wohnungssuche lists a few listings per page (``article.apartment`` cards)
and paginates via Solr: ``a.page-link`` hrefs carry ``tx_solr[page]=N``
(URL-encoded ``tx_solr%5Bpage%5D=N``). We follow the link to the next page
until a page yields nothing new, there is no link to the next page, or the page
cap is reached. The listing id is derived from the detail URL
(``listing_id``); ``data-apartment-uid`` is not stable.

The card's € figure is the "Miete inkl. NK" (warm rent) from the detail page,
so it is surfaced as both ``price`` and ``price_warm``; the card carries no
cold rent.
"""
import re
from typing import Optional

from bs4 import BeautifulSoup, Tag

from flathunter.abstract_crawler import Crawler
from flathunter.logging import logger

from berlin_flat_hunter.crawlers.common import (
    absolute_url,
    amount_or_empty,
    listing_id,
    normalise,
    str_attr,
)

BASE_URL = "https://www.gesobau.de"

# Hard ceiling on pages per crawl when the caller passes no ``max_pages``.
# Gesobau shows ~6 listings/page and ~11 in total, so hitting this means
# pagination is broken, not that it's busy.
_MAX_PAGES = 20


class Gesobau(Crawler):

    URL_PATTERN = re.compile(r"https://www\.gesobau\.de")

    def get_results(self, search_url: str, max_pages: Optional[int] = None) -> list:
        entries: list = []
        seen: set = set()
        page_cap = max_pages if max_pages is not None else _MAX_PAGES
        url = search_url
        page = 1
        while True:
            soup = self.get_soup_from_url(url)
            new = 0
            for entry in self.extract_data(soup):
                if entry["id"] in seen:
                    continue
                seen.add(entry["id"])
                entries.append(entry)
                new += 1
            if not new:
                break
            next_url = self._next_page_url(soup, page + 1)
            if not next_url:
                break
            if page >= page_cap:
                if max_pages is None:
                    logger.warning("Gesobau: stopped at the %d-page cap with every page "
                                   "still returning new listings — pagination may be broken",
                                   _MAX_PAGES)
                break
            url = next_url
            page += 1
        return entries

    @staticmethod
    def _next_page_url(soup: BeautifulSoup, next_page: int) -> Optional[str]:
        if not isinstance(soup, Tag):
            return None
        pattern = re.compile(rf"tx_solr(?:%5B|\[)page(?:%5D|\])={next_page}(?!\d)")
        for link in soup.select("a.page-link[href]"):
            href = str_attr(link, "href")
            if pattern.search(href):
                return absolute_url(BASE_URL, href)
        return None

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

    def _parse_item(self, item: Tag) -> Optional[dict]:
        # Only real listing cards carry data-apartment-uid; the page also renders
        # Neubau/Senioren promo teasers as ``article.apartment``. The uid changes
        # for the same listing, so it is only this marker — the id is the URL's.
        if not str_attr(item, "data-apartment-uid"):
            return None
        title_link = item.select_one("h3.basicTeaser__title a")
        if not title_link or not isinstance(title_link, Tag):
            return None
        title = normalise(title_link.get_text(" ", strip=True))
        url = absolute_url(BASE_URL, str_attr(title_link, "href"))
        if not url:
            return None

        district_el = item.select_one(".meta__region")
        district = normalise(district_el.get_text(" ", strip=True)) if district_el else ""
        addr_el = item.select_one(".basicTeaser__text span")
        address_part = normalise(addr_el.get_text(" ", strip=True)) if addr_el else ""
        address = f"{address_part}, {district}".strip(", ") if district else address_part

        # apartment__info contains 3 spans: price, rooms, size
        info = item.find(class_="apartment__info")
        price = rooms = size = ""
        if info and isinstance(info, Tag):
            spans = [normalise(s.get_text(" ", strip=True)) for s in info.find_all("span")
                     if s.get_text(strip=True)]
            for s in spans:
                if "€" in s and not price:
                    price = amount_or_empty(s)
                elif "Zimmer" in s and not rooms:
                    rooms = s
                elif "m²" in s and not size:
                    size = s

        img_el = item.select_one("img")
        img_src = normalise(str_attr(img_el, "src")) if img_el and isinstance(img_el, Tag) else ""
        image = absolute_url(BASE_URL, img_src)

        return {
            "id": listing_id(url),
            "url": url,
            "image": image,
            "title": title,
            "address": address,
            "rooms": rooms,
            "size": size,
            "price": price,
            "price_cold": "",
            "price_warm": price,
            "crawler": self.get_name(),
        }
