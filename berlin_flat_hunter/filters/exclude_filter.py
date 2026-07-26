"""ExcludeFilter — drop junk listings by keyword, matching title+description+address.

Ports the pi bot's default exclude shield (senior/assisted-living, sublet/
takeover, flat-swaps, WG rooms, commercial, furnished, parking-only) plus the
user's own exclude keywords. Unlike flathunter's built-in ``excluded_titles``
(TitleFilter, title-only regex), this matches the whole haystack — title,
description AND address — as case-insensitive substrings, so a tell that only
appears in the description still drops the listing.

Config (per profile):

    exclude:
      use_defaults: true          # apply the curated shield (default true)
      keywords: ["deposit via"]   # extra user substrings

For backward compat it also picks up flathunter ``excluded_titles`` as user
keywords when no ``exclude.keywords`` is set.
"""
import re
from typing import Iterator, Optional

from flathunter.abstract_processor import Processor
from flathunter.logging import logger

from berlin_flat_hunter.filters.keywords import (
    DEFAULT_EXCLUDE_KEYWORDS,
    DEFAULT_EXCLUDE_PARKING_KEYWORDS,
    DEFAULT_EXCLUDE_TITLE_KEYWORDS,
    TITLE_EXCLUDE_GUARDS,
)

_HAS_ROOM = re.compile(r"[1-9]")


class ExcludeFilter(Processor):
    def __init__(self, config):
        cfg = config.exclude_filter_config() if hasattr(config, "exclude_filter_config") else {}
        use_defaults = bool(cfg.get("use_defaults", True))
        user_kw = [str(k).lower().strip() for k in (cfg.get("keywords") or []) if str(k).strip()]
        base = list(DEFAULT_EXCLUDE_KEYWORDS) if use_defaults else []
        # dedup, keep order
        seen: set[str] = set()
        self._keywords = tuple(k for k in user_kw + base if k and not (k in seen or seen.add(k)))
        self._title_keywords = tuple(DEFAULT_EXCLUDE_TITLE_KEYWORDS) if use_defaults else ()
        self._parking_keywords = tuple(DEFAULT_EXCLUDE_PARKING_KEYWORDS) if use_defaults else ()

    def process_exposes(self, exposes) -> Iterator[dict]:  # type: ignore[override]
        for expose in exposes:
            reason = self._excluded(expose)
            if reason is None:
                yield expose
            else:
                logger.info("ExcludeFilter: dropping %s — matched %r",
                            expose.get("url"), reason)

    def _excluded(self, expose: dict) -> Optional[str]:
        title = (expose.get("title") or "")
        haystack = " ".join(filter(None, [
            title, expose.get("description", ""), expose.get("address", ""),
        ])).lower()
        if not haystack:
            return None
        for kw in self._keywords:
            if kw in haystack:
                return kw
        title_low = title.lower()
        for kw in self._title_keywords:
            if kw in title_low and not any(g in title_low for g in TITLE_EXCLUDE_GUARDS.get(kw, ())):
                return kw
        # Parking-only: exclude those words only when the listing has no rooms
        # (a real flat has >= 1 room; a Stellplatz/Garage listing has none).
        if not _HAS_ROOM.search(expose.get("rooms", "") or ""):
            for kw in self._parking_keywords:
                if kw in haystack:
                    return kw
        return None
