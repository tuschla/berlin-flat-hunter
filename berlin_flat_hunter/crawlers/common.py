"""Helpers shared by the Berlin public-housing crawlers."""
import re
import zlib
from typing import Optional

import requests
# Patches requests.Session to start with a random browser User-Agent. Imported
# here, before SESSION exists: with the default python-requests agent Howoge
# redirects the JSON endpoint to its crawler trap and the crawl returns nothing.
import requests_random_user_agent  # noqa: F401
from bs4 import Tag

from flathunter.logging import logger

# One module-level Session for every crawler: connection pooling + keep-alive
# across crawls. Thread-safe enough for Gewobag's concurrent detail GETs.
SESSION = requests.Session()

_WHITESPACE_RE = re.compile(r"\s+")
# A real amount contains a digit; teaser listings show "Auf Anfrage" instead.
_DIGIT_RE = re.compile(r"\d")


def url_slug(url: str) -> str:
    """Last path segment of a detail URL (query included), the listing's natural key."""
    return url.rstrip("/").rsplit("/", 1)[-1] or url


def listing_id(url: str) -> int:
    """Stable listing id: crc32 of the detail-URL slug.

    The site-provided uids (WBM data-uid, Gewobag post id, Howoge uid, Gesobau
    data-apartment-uid, Degewo bookmark uid) all change for the same listing,
    so every crawler derives its id from the URL instead. This is the one place
    that decides listing identity; flathunter's ``processed`` table and the
    stats DB key on it.
    """
    return zlib.crc32(url_slug(url).encode("utf-8"))


def absolute_url(base: str, href: str) -> str:
    """Resolve a scraped ``href`` against the site root ``base`` (no trailing slash)."""
    if not href:
        return ""
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return f"https:{href}"
    return base + href if href.startswith("/") else f"{base}/{href}"


def normalise(text: str) -> str:
    """Collapse all internal whitespace runs (incl. tabs/newlines) into single spaces."""
    return _WHITESPACE_RE.sub(" ", text).strip()


def str_attr(tag: Tag, name: str) -> str:
    """Safely return a string attribute (BeautifulSoup may return a list for multi-value attrs)."""
    value = tag.get(name, "")
    if isinstance(value, list):
        return value[0] if value else ""
    return value if isinstance(value, str) else ""


def amount_or_empty(text: str) -> str:
    """``text`` if it carries an amount (a digit), else "" — so a placeholder like
    "Auf Anfrage" never lands in a price field and blocks the stats backfill."""
    return text if _DIGIT_RE.search(text) else ""


def format_amount(value, suffix: str) -> str:
    """Format a number with a unit suffix. Returns "" when the value is
    missing/zero so schema_monitor can flag it."""
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


def get_with_retry(url: str, *, label: str, headers: dict, timeout: float,
                   params: Optional[dict] = None) -> Optional[requests.Response]:
    """GET ``url`` with one retry on 5xx / transient network failure."""
    return _request_with_retry("GET", url, label=label, headers=headers,
                               timeout=timeout, params=params)


def post_with_retry(url: str, *, label: str, headers: dict, timeout: float,
                    data: dict) -> Optional[requests.Response]:
    """POST ``url`` with one retry on 5xx / transient network failure."""
    return _request_with_retry("POST", url, label=label, headers=headers,
                               timeout=timeout, data=data)


def _request_with_retry(method: str, url: str, *, label: str, headers: dict,
                        timeout: float, **kwargs) -> Optional[requests.Response]:
    """Returns the final Response (which may still be non-200) or None on hard
    failure. A single transient 5xx blip would otherwise read as an empty crawl
    and tick the schema monitor's false-alarm ladder."""
    for attempt in range(2):
        try:
            resp = SESSION.request(method, url, headers=headers, timeout=timeout, **kwargs)
        except requests.exceptions.RequestException as exc:
            if attempt == 0:
                logger.debug("%s: transient %s error for %s, retrying: %s",
                             label, method, url, exc)
                continue
            logger.warning("%s: could not fetch %s: %s", label, url, exc)
            return None
        if 500 <= resp.status_code < 600 and attempt == 0:
            continue
        return resp
    return None
