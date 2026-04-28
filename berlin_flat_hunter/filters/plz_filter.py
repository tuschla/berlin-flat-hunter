"""PlzFilter — keep only exposes whose Berlin postal code is in a configured neighborhood.

Berlin postal codes (10115–14199) cover the whole city without gaps, so any
listing whose address contains a 5-digit PLZ either matches a configured
neighborhood or doesn't — there are no edge-of-polygon dead zones.

Config shape (top-level)::

    neighborhoods:
      Wrangelkiez:    [10997]
      Reuterkiez:     [12047]
      Friedrichshain: [10243, 10245, 10247, 10249]

A matched expose gets ``expose["matched_neighborhood"]`` set to the name.
"""
import re
from typing import Iterator, Optional

from flathunter.abstract_processor import Processor
from flathunter.logging import logger

# Berlin PLZ block: 10115–14199. Match a 5-digit number not embedded in a longer run.
_PLZ_RE = re.compile(r"(?<!\d)(\d{5})(?!\d)")


class PlzFilter(Processor):
    """Drop exposes whose address PLZ isn't in any configured neighborhood."""

    def __init__(self, config):
        mapping = config.neighborhood_plz() if hasattr(config, "neighborhood_plz") else {}
        if not mapping:
            raise ValueError("neighborhoods: must contain at least one name → [PLZ, ...] entry")

        # Reverse-index: PLZ → name. First name wins on collisions (warn).
        self._plz_to_name: dict[str, str] = {}
        for name, codes in mapping.items():
            for code in codes:
                if not (isinstance(code, str) and code.isdigit() and len(code) == 5):
                    raise ValueError(
                        f"neighborhoods.{name}: PLZ {code!r} must be a 5-digit string"
                    )
                if code in self._plz_to_name and self._plz_to_name[code] != name:
                    logger.warning(
                        "PlzFilter: PLZ %s assigned to both %r and %r — keeping %r",
                        code, self._plz_to_name[code], name, self._plz_to_name[code],
                    )
                    continue
                self._plz_to_name[code] = name

    def process_exposes(self, exposes) -> Iterator[dict]:  # type: ignore[override]
        for expose in exposes:
            name = self._match(expose)
            if name is None:
                continue
            expose["matched_neighborhood"] = name
            yield expose

    def _match(self, expose: dict) -> Optional[str]:
        address = expose.get("address", "")
        plz = self._extract_plz(address)
        if plz is None:
            logger.debug("PlzFilter: no PLZ in address %r — dropping %s",
                        address, expose.get("url"))
            return None
        return self._plz_to_name.get(plz)

    @staticmethod
    def _extract_plz(address: str) -> Optional[str]:
        m = _PLZ_RE.search(address)
        return m.group(1) if m else None
