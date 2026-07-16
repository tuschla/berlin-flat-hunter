"""Gewobag crawler — WP REST API for listing index, HTML scraping for details"""
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests
from bs4 import BeautifulSoup, Tag

from flathunter.abstract_crawler import Crawler
from flathunter.logging import logger

# Module-level Session: connection pooling + keep-alive across crawls.
# requests.Session() is thread-safe enough for concurrent GETs to the same host.
_SESSION = requests.Session()

WP_API_URL = "https://www.gewobag.de/wp-json/wp/v2/immobilien"

# Substrings (lowercased, ä→ae normalised) that mark a non-apartment listing.
# German plurals umlautify the stem (Stellplatz → Stellplätze → "stellplaetze"),
# so we need both the singular and the plural stem. We also intentionally
# match these as plain substrings — *not* word-boundary regex — because German
# noun compounding glues stems onto a head ("Behindertenparkplatz",
# "Tiefgaragenstellplatz") and the stem we want to skip on can land anywhere
# inside the compound. Real Gewobag listing titles are noun phrases (not verb
# forms like "Aufladen"), so the loose match has a near-zero false-positive
# rate in practice.
_SKIP_KEYWORDS = frozenset([
    "stellplatz", "stellplaetz",
    "parkplatz", "parkplaetz",
    "parkhaus", "parkhaeus",
    "tiefgarage", "garage",
    "fahrradkeller", "fahrradraum", "mofa",
    "gewerbe", "buero", "praxis", "laden", "lager",
])

_WHITESPACE_RE = re.compile(r"\s+")
# A real rent value contains a digit; teaser listings show "Auf Anfrage" here.
_HAS_AMOUNT = re.compile(r"\d")


def _normalise(text: str) -> str:
    """Collapse all internal whitespace runs (incl. tabs/newlines) into single spaces."""
    return _WHITESPACE_RE.sub(" ", text).strip()


class Gewobag(Crawler):

    URL_PATTERN = re.compile(r"https://www\.gewobag\.de")

    def get_results(self, search_url: str, max_pages: Optional[int] = None) -> list:
        items = self._fetch_index(max_pages)
        apartments = [i for i in items if self._is_apartment(i)]
        entries: list = []
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(self._fetch_detail, item): item for item in apartments}
            for future in as_completed(futures):
                try:
                    entry = future.result()
                    if entry:
                        entries.append(entry)
                except Exception as exc:
                    logger.warning("Gewobag detail fetch failed: %s", exc)
        return entries

    def _fetch_index(self, max_pages: Optional[int]) -> list:
        items: list = []
        page = 1
        while True:
            resp = self._fetch_with_retry(
                WP_API_URL,
                params={"per_page": 100, "page": page, "status": "publish"},
            )
            if resp is None or resp.status_code != 200:
                break
            try:
                batch = resp.json()
            except ValueError as exc:
                logger.warning("Gewobag index returned invalid JSON: %s", exc)
                break
            if not batch:
                break
            items.extend(batch)
            if max_pages is not None and page >= max_pages:
                break
            if len(batch) < 100:
                break
            page += 1
        return items

    def _is_apartment(self, item: dict) -> bool:
        title = self._normalise_german(self._title(item).lower())
        return not any(kw in title for kw in _SKIP_KEYWORDS)

    @staticmethod
    def _title(item: dict) -> str:
        """Defensive title extraction — WP API may serialize title as None or string."""
        raw = item.get("title")
        if isinstance(raw, dict):
            value = raw.get("rendered", "")
            return value if isinstance(value, str) else ""
        return raw if isinstance(raw, str) else ""

    @staticmethod
    def _normalise_german(text: str) -> str:
        """Replace umlauts with ASCII so substring matching catches plurals and variants."""
        return (text
                .replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss"))

    def _fetch_with_retry(self, url: str, params: Optional[dict] = None):
        """GET ``url`` with one retry on 5xx / transient network failure.
        Returns the final Response (which may still be non-200) or None on hard failure.
        """
        for attempt in range(2):
            try:
                resp = _SESSION.get(url, params=params, headers=self.HEADERS, timeout=20)
            except requests.exceptions.RequestException as exc:
                if attempt == 0:
                    logger.debug("Gewobag transient %s fetch error, retrying: %s", url, exc)
                    continue
                logger.warning("Gewobag could not fetch %s: %s", url, exc)
                return None
            if 500 <= resp.status_code < 600 and attempt == 0:
                continue  # one retry for transient server errors
            return resp
        return None

    def _fetch_detail(self, item: dict) -> Optional[dict]:
        url = item.get("link", "")
        uid = item.get("id")
        title = self._title(item)
        if not url or uid is None:
            return None
        resp = self._fetch_with_retry(url)
        if resp is None or resp.status_code != 200:
            if resp is not None:
                logger.debug("Gewobag detail %s returned HTTP %d", url, resp.status_code)
            return None
        # lxml parser is significantly faster than html.parser and is already
        # required by flathunter; reuse it here too.
        soup = BeautifulSoup(resp.content, "lxml")

        grundmiete = gesamtmiete = address = rooms = size = ""
        for table in soup.find_all("table", class_="overview-table"):
            if not isinstance(table, Tag):
                continue
            for row in table.find_all("tr"):
                th = row.find("th")
                td = row.find("td")
                if not th or not td:
                    continue
                label = _normalise(th.get_text(" ", strip=True)).lower()
                value = _normalise(td.get_text(" ", strip=True))
                if "grundmiete" in label:
                    grundmiete = value
                elif "gesamtmiete" in label:
                    gesamtmiete = value
                elif "anschrift" in label:
                    address = value
                elif "anzahl zimmer" in label:
                    rooms = value
                elif "fläche" in label:
                    size = value

        # Prefer Grundmiete (Kaltmiete) over Gesamtmiete regardless of row order
        price = grundmiete or gesamtmiete
        # Keep the cold/warm split for data analysis, but only when the value is
        # a real amount — teaser listings render "Auf Anfrage" here, and storing
        # that would block the re-sight backfill once the real figure appears.
        price_cold = grundmiete if _HAS_AMOUNT.search(grundmiete) else ""
        price_warm = gesamtmiete if _HAS_AMOUNT.search(gesamtmiete) else ""

        og_img = soup.find("meta", property="og:image")
        image = ""
        if og_img and isinstance(og_img, Tag):
            content = og_img.get("content", "")
            image = content if isinstance(content, str) else ""

        return {
            "id": uid,
            "url": url,
            "title": title,
            "address": address,
            "rooms": rooms,
            "size": size,
            "price": price,
            "price_cold": price_cold,
            "price_warm": price_warm,
            "image": image,
            "crawler": self.get_name(),
        }

    def extract_data(self, raw_data):
        raise NotImplementedError
