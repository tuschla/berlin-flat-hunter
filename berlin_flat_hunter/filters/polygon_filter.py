"""PolygonFilter — keep only exposes whose address falls inside a configured polygon"""
import time
from collections import OrderedDict
from typing import Iterator, Optional

from flathunter.abstract_processor import Processor
from flathunter.logging import logger

_NOMINATIM_MIN_INTERVAL = 1.1  # seconds between requests (Nominatim policy: max 1 req/sec)
_CACHE_MAX = 4096  # cap geocode cache to bound memory in long-running loops


class PolygonFilter(Processor):
    """Geocode expose addresses; drop any whose coordinates fall outside the polygon.

    Requires shapely and geopy. Geocoding uses Nominatim (free, no API key).
    Results are cached per-instance.
    """

    def __init__(self, config):
        try:
            from shapely.geometry import Point, Polygon as ShapelyPolygon
        except ImportError as exc:
            raise ImportError("shapely is required for PolygonFilter: pip install shapely") from exc
        try:
            from geopy.geocoders import Nominatim
        except ImportError as exc:
            raise ImportError("geopy is required for PolygonFilter: pip install geopy") from exc

        coords = config.search_polygon() if hasattr(config, "search_polygon") else []
        if len(coords) < 3:
            raise ValueError("search_area.polygon needs at least 3 [lat, lon] points")
        for i, point in enumerate(coords):
            if not (isinstance(point, (list, tuple)) and len(point) == 2):
                raise ValueError(f"search_area.polygon[{i}] must be [lat, lon]; got {point!r}")
            lat, lon = point
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError(
                    f"search_area.polygon[{i}] = [{lat}, {lon}] is out of range "
                    "(lat must be -90..90, lon -180..180 — did you swap them?)"
                )

        self._Point = Point  # used per-request in _within_polygon
        # shapely uses (x=lon, y=lat)
        self.polygon = ShapelyPolygon([(lon, lat) for lat, lon in coords])
        self._geocoder = Nominatim(user_agent="berlin-flat-hunter/1.0")
        # LRU: bounded so a long-running loop with many unique addresses
        # cannot grow the cache without limit.
        self._cache: "OrderedDict[str, Optional[tuple[float, float]]]" = OrderedDict()
        self._last_geocode_ts: float = 0.0

    def process_exposes(self, exposes) -> Iterator[dict]:  # type: ignore[override]
        for expose in exposes:
            if self._within_polygon(expose):
                yield expose

    def _within_polygon(self, expose: dict) -> bool:
        address = expose.get("address", "").strip()
        if not address:
            logger.debug("PolygonFilter: no address for %s, keeping", expose.get("url"))
            return True

        if address in self._cache:
            self._cache.move_to_end(address)  # mark as recently used
            coords = self._cache[address]
        else:
            coords = self._geocode(address)
            self._cache[address] = coords
            if len(self._cache) > _CACHE_MAX:
                self._cache.popitem(last=False)  # evict oldest

        if coords is None:
            logger.warning("PolygonFilter: could not geocode '%s', keeping", address)
            return True

        lat, lon = coords
        inside = self.polygon.contains(self._Point(lon, lat))
        logger.debug("PolygonFilter: %s (%s) → %s", address, coords, "IN" if inside else "OUT")
        return inside

    def _geocode(self, address: str) -> Optional[tuple[float, float]]:
        # Rate-limit Nominatim: sleep only if we hit it too recently.
        # Use monotonic so wall-clock changes cannot break the rate limit.
        elapsed = time.monotonic() - self._last_geocode_ts
        if elapsed < _NOMINATIM_MIN_INTERVAL:
            time.sleep(_NOMINATIM_MIN_INTERVAL - elapsed)
        try:
            location = self._geocoder.geocode(address, timeout=10)  # type: ignore[arg-type]
        except Exception as exc:
            logger.warning("Geocoding failed for '%s': %s", address, exc)
            self._last_geocode_ts = time.monotonic()
            return None
        self._last_geocode_ts = time.monotonic()
        if location is None:
            return None
        return (float(location.latitude), float(location.longitude))  # type: ignore[attr-defined]
