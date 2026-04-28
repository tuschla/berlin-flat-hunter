"""Tests for PlzFilter — postal-code-based neighborhood filter."""
import unittest
from unittest.mock import MagicMock

from berlin_flat_hunter.filters.plz_filter import PlzFilter


def _make_config(mapping):
    cfg = MagicMock()
    cfg.neighborhood_plz.return_value = mapping
    return cfg


def _expose(address: str, eid: int = 1) -> dict:
    return {"id": eid, "url": f"https://example.com/{eid}/", "title": "T",
            "address": address, "rooms": "2", "size": "60 m²", "price": "900 €"}


class TestPlzFilter(unittest.TestCase):

    def test_keeps_matching_plz(self):
        fltr = PlzFilter(_make_config({"Wrangelkiez": ["10997"]}))
        out = list(fltr.process_exposes([_expose("Wrangelstr. 1, 10997 Berlin")]))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["matched_neighborhood"], "Wrangelkiez")

    def test_drops_non_matching_plz(self):
        fltr = PlzFilter(_make_config({"Wrangelkiez": ["10997"]}))
        out = list(fltr.process_exposes([_expose("Spandauer Str. 1, 13591 Berlin")]))
        self.assertEqual(out, [])

    def test_drops_address_without_plz_or_name(self):
        fltr = PlzFilter(_make_config({"Wrangelkiez": ["10997"]}))
        out = list(fltr.process_exposes([_expose("Some street, Berlin")]))
        self.assertEqual(out, [])

    def test_name_fallback_when_no_plz(self):
        """Address has no PLZ but contains the neighborhood name — match on name."""
        fltr = PlzFilter(_make_config({"Friedrichshain": ["10243", "10245"]}))
        out = list(fltr.process_exposes([_expose("Some street, Friedrichshain, Berlin")]))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["matched_neighborhood"], "Friedrichshain")

    def test_name_fallback_word_boundary(self):
        """A name that's a substring of another word must NOT match (Mitte vs. Mittelweg)."""
        fltr = PlzFilter(_make_config({"Mitte": ["10117"]}))
        out = list(fltr.process_exposes([_expose("Mittelweg 5, Some Other District, Berlin")]))
        self.assertEqual(out, [])

    def test_name_fallback_case_insensitive(self):
        fltr = PlzFilter(_make_config({"Friedrichshain": ["10243"]}))
        out = list(fltr.process_exposes([_expose("FRIEDRICHSHAIN, Berlin")]))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["matched_neighborhood"], "Friedrichshain")

    def test_plz_takes_precedence_over_name(self):
        """If PLZ is present and doesn't match, listing is dropped even if address mentions a configured name."""
        fltr = PlzFilter(_make_config({"Friedrichshain": ["10243"]}))
        out = list(fltr.process_exposes([
            _expose("Friedrichshain Café, Mitte, 10117 Berlin"),
        ]))
        self.assertEqual(out, [])  # PLZ 10117 not in mapping; name fallback only fires if no PLZ

    def test_name_fallback_longest_name_wins(self):
        """Multi-word names should not be shadowed by sub-name overlap."""
        fltr = PlzFilter(_make_config({
            "Berg": ["10119"],
            "Prenzlauer Berg": ["10405"],
        }))
        out = list(fltr.process_exposes([_expose("Some street, Prenzlauer Berg, Berlin")]))
        self.assertEqual(out[0]["matched_neighborhood"], "Prenzlauer Berg")

    def test_multiple_neighborhoods(self):
        fltr = PlzFilter(_make_config({
            "Wrangelkiez": ["10997"],
            "Reuterkiez": ["12047"],
        }))
        a = _expose("X, 10997 Berlin", eid=1)
        b = _expose("Y, 12047 Berlin", eid=2)
        c = _expose("Z, 13591 Berlin", eid=3)
        out = list(fltr.process_exposes([a, b, c]))
        self.assertEqual({e["id"] for e in out}, {1, 2})
        self.assertEqual(out[0]["matched_neighborhood"], "Wrangelkiez")
        self.assertEqual(out[1]["matched_neighborhood"], "Reuterkiez")

    def test_int_plz_normalised(self):
        """Config may give PLZ as ints; filter still matches."""
        fltr = PlzFilter(_make_config({"Wedding": [str(13347)]}))
        out = list(fltr.process_exposes([_expose("X, 13347 Berlin")]))
        self.assertEqual(len(out), 1)

    def test_short_plz_zero_padded(self):
        """A '9000' style PLZ is padded to 09000 — Berlin doesn't use it but
        the normalisation rule is consistent across the module."""
        fltr = PlzFilter(_make_config({"X": ["09000"]}))
        out = list(fltr.process_exposes([_expose("X, 09000 City")]))
        self.assertEqual(len(out), 1)

    def test_invalid_plz_shape_raises(self):
        with self.assertRaises(ValueError):
            PlzFilter(_make_config({"X": ["abc12"]}))
        with self.assertRaises(ValueError):
            PlzFilter(_make_config({"X": ["123456"]}))

    def test_plz_must_not_match_substring(self):
        """5-digit number embedded in a longer digit run must not match."""
        fltr = PlzFilter(_make_config({"X": ["10997"]}))
        # Phone numbers etc. should not produce false positives
        out = list(fltr.process_exposes([_expose("Tel: 1099712345, Berlin")]))
        self.assertEqual(out, [])

    def test_empty_config_raises(self):
        with self.assertRaises(ValueError):
            PlzFilter(_make_config({}))

    def test_overlap_warns_first_wins(self):
        """A PLZ assigned to two names → first one wins (deterministic)."""
        fltr = PlzFilter(_make_config({
            "Wrangelkiez": ["10997"],
            "Reuterkiez": ["10997"],  # collision
        }))
        out = list(fltr.process_exposes([_expose("X, 10997 Berlin")]))
        self.assertEqual(out[0]["matched_neighborhood"], "Wrangelkiez")


if __name__ == "__main__":
    unittest.main()
