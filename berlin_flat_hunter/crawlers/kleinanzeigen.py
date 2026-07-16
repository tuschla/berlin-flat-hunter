"""Kleinanzeigen crawler — subclass that repairs size/rooms extraction and
enriches exposes with the description snippet the list page already carries.

Two problems with flathunter's upstream Kleinanzeigen crawler as of 2026-07:

1. It reads size/rooms from ``.simpletag`` elements, which Kleinanzeigen has
   removed. ``tags[0]`` now resolves to the seller-type badge ("Von Privat"),
   so ``size`` is garbage and ``rooms`` is always empty. Size + area now live
   in a single ``.aditem-main--middle--tags`` block as ``"72,13 m² · 2 Zi."``.

2. The ``.aditem-main--middle--description`` snippet is discarded, robbing the
   ScamFilter of its best signal — scam templates put "currently abroad",
   "WhatsApp", email addresses, etc. in the description body, not the title.

We reuse upstream's ``extract_data`` for the fields it still gets right
(id/url/title/price/address/image) and, in a second pass keyed by
``data-adid``, overwrite size + rooms and attach description. No extra HTTP
requests — everything ships with the list page.
"""
import re

from bs4 import BeautifulSoup, Tag

from flathunter.crawler.kleinanzeigen import Kleinanzeigen as _UpstreamKleinanzeigen

# "72,13 m²" / "28 m²" — German or plain decimal, tolerant of the trailing unit.
_SIZE_RE = re.compile(r"\d+(?:[.,]\d+)?\s*m²")
# "2 Zi." / "1,5 Zi" — room count preceding the Zimmer abbreviation.
_ROOMS_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*Zi\.?", re.IGNORECASE)


class Kleinanzeigen(_UpstreamKleinanzeigen):
    """Kleinanzeigen crawler: fixes size/rooms and captures the description."""

    def extract_data(self, raw_data: BeautifulSoup) -> list:
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
