"""Expose-id coercion: keep flathunter's int(id) from crashing on string ids."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from berlin_flat_hunter.hunter import _coerce_expose_id  # noqa: E402


def test_int_id_untouched():
    e = {"id": 123, "url": "u"}
    _coerce_expose_id(e)
    assert e["id"] == 123


def test_numeric_string_untouched():
    e = {"id": "456", "url": "u"}
    _coerce_expose_id(e)
    assert int(e["id"]) == 456


def test_string_id_coerced_stable_and_int():
    e1 = {"id": "W1300.42303.0131-0504", "url": "u"}
    e2 = dict(e1)
    _coerce_expose_id(e1)
    _coerce_expose_id(e2)
    assert isinstance(e1["id"], int) and e1["id"] >= 0
    assert e1["id"] == e2["id"]           # stable → cross-cycle dedup preserved
    assert int(e1["id"]) == e1["id"]      # int-coercible (won't crash save_expose)


def test_missing_id_falls_back_to_url():
    e = {"url": "http://x/1"}
    _coerce_expose_id(e)
    assert isinstance(e["id"], int)


def test_idempotent():
    e = {"id": "slug-abc", "url": "u"}
    _coerce_expose_id(e)
    first = e["id"]
    _coerce_expose_id(e)
    assert e["id"] == first
