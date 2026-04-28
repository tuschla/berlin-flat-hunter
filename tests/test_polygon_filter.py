"""Tests for PolygonFilter"""
import unittest
from unittest.mock import MagicMock, patch

from berlin_flat_hunter.filters.polygon_filter import PolygonFilter

# Small triangle around central Berlin (Mitte area)
BERLIN_MITTE_POLYGON = [
    [52.535, 13.370],
    [52.535, 13.430],
    [52.500, 13.430],
    [52.500, 13.370],
]

EXPOSE_IN = {
    "id": 1, "url": "https://example.com/1/", "title": "Wohnung Mitte",
    "address": "Alexanderstraße 1, 10178 Berlin", "rooms": "2", "size": "60 m²", "price": "900 €",
}
EXPOSE_OUT = {
    "id": 2, "url": "https://example.com/2/", "title": "Wohnung Spandau",
    "address": "Eidechsenweg 9, 13591 Berlin", "rooms": "3", "size": "72 m²", "price": "1100 €",
}
EXPOSE_NO_ADDR = {
    "id": 3, "url": "https://example.com/3/", "title": "Wohnung ohne Adresse",
    "address": "", "rooms": "2", "size": "55 m²", "price": "800 €",
}


def _make_config(polygon=None):
    cfg = MagicMock()
    cfg.search_polygon.return_value = polygon or BERLIN_MITTE_POLYGON
    return cfg


def _make_filter(polygon=None):
    return PolygonFilter(_make_config(polygon))


class TestPolygonFilter(unittest.TestCase):

    @patch("berlin_flat_hunter.filters.polygon_filter.PolygonFilter._geocode")
    def test_expose_inside_kept(self, mock_geocode):
        mock_geocode.return_value = (52.521, 13.404)  # inside polygon
        fltr = _make_filter()
        results = list(fltr.process_exposes([EXPOSE_IN]))
        self.assertEqual(len(results), 1)

    @patch("berlin_flat_hunter.filters.polygon_filter.PolygonFilter._geocode")
    def test_expose_outside_dropped(self, mock_geocode):
        mock_geocode.return_value = (52.535, 13.200)  # outside polygon (west)
        fltr = _make_filter()
        results = list(fltr.process_exposes([EXPOSE_OUT]))
        self.assertEqual(len(results), 0)

    @patch("berlin_flat_hunter.filters.polygon_filter.PolygonFilter._geocode")
    def test_no_address_kept(self, mock_geocode):
        fltr = _make_filter()
        results = list(fltr.process_exposes([EXPOSE_NO_ADDR]))
        self.assertEqual(len(results), 1)
        mock_geocode.assert_not_called()

    @patch("berlin_flat_hunter.filters.polygon_filter.PolygonFilter._geocode")
    def test_geocode_failure_keeps_expose(self, mock_geocode):
        mock_geocode.return_value = None
        fltr = _make_filter()
        results = list(fltr.process_exposes([EXPOSE_IN]))
        self.assertEqual(len(results), 1)

    @patch("berlin_flat_hunter.filters.polygon_filter.PolygonFilter._geocode")
    def test_multiple_exposes_filtered(self, mock_geocode):
        mock_geocode.side_effect = [
            (52.521, 13.404),   # inside
            (52.535, 13.200),   # outside
        ]
        fltr = _make_filter()
        results = list(fltr.process_exposes([EXPOSE_IN, EXPOSE_OUT]))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], EXPOSE_IN["id"])

    @patch("berlin_flat_hunter.filters.polygon_filter.PolygonFilter._geocode")
    def test_geocode_called_with_address(self, mock_geocode):
        mock_geocode.return_value = (52.521, 13.404)
        fltr = _make_filter()
        list(fltr.process_exposes([EXPOSE_IN]))
        mock_geocode.assert_called_once_with(EXPOSE_IN["address"])

    @patch("berlin_flat_hunter.filters.polygon_filter.PolygonFilter._geocode")
    def test_cache_prevents_double_geocode(self, mock_geocode):
        mock_geocode.return_value = (52.521, 13.404)
        fltr = _make_filter()
        # Inject into cache manually
        fltr._cache[EXPOSE_IN["address"]] = (52.521, 13.404)
        list(fltr.process_exposes([EXPOSE_IN]))
        mock_geocode.assert_not_called()

    def test_too_few_points_raises(self):
        with self.assertRaises(ValueError):
            _make_filter(polygon=[[52.5, 13.4], [52.6, 13.4]])

    @patch("berlin_flat_hunter.filters.polygon_filter._CACHE_MAX", 2)
    @patch("berlin_flat_hunter.filters.polygon_filter.PolygonFilter._geocode")
    def test_cache_evicts_oldest_when_full(self, mock_geocode):
        """LRU cap: oldest entry evicted once cache exceeds _CACHE_MAX."""
        mock_geocode.return_value = (52.521, 13.404)
        fltr = _make_filter()
        a = dict(EXPOSE_IN, address="A")
        b = dict(EXPOSE_IN, address="B")
        c = dict(EXPOSE_IN, address="C")
        list(fltr.process_exposes([a, b, c]))
        self.assertEqual(len(fltr._cache), 2)
        self.assertNotIn("A", fltr._cache)  # oldest evicted
        self.assertIn("B", fltr._cache)
        self.assertIn("C", fltr._cache)

    def test_swapped_lat_lon_raises(self):
        """Common mistake: passing [lon, lat] instead of [lat, lon]."""
        with self.assertRaises(ValueError):
            _make_filter(polygon=[[200, 13], [52, 13], [52, 14]])

    def test_malformed_point_raises(self):
        with self.assertRaises(ValueError):
            _make_filter(polygon=[[52.5], [52.6, 13.4], [52.5, 13.5]])

    def test_missing_shapely_raises(self):
        import sys
        orig = sys.modules.get("shapely")
        sys.modules["shapely"] = None  # type: ignore[assignment]
        sys.modules["shapely.geometry"] = None  # type: ignore[assignment]
        try:
            with self.assertRaises(ImportError):
                PolygonFilter(_make_config())
        finally:
            if orig is not None:
                sys.modules["shapely"] = orig
            else:
                del sys.modules["shapely"]
            if "shapely.geometry" in sys.modules:
                del sys.modules["shapely.geometry"]


if __name__ == "__main__":
    unittest.main()
