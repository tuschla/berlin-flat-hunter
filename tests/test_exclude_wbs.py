"""ExcludeFilter (junk shield, desc-matching) + WbsFilter (with negation guard)
+ per-profile source gating."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from flathunter.idmaintainer import IdMaintainer  # noqa: E402

from berlin_flat_hunter.config import BerlinConfig  # noqa: E402
from berlin_flat_hunter.filters.exclude_filter import ExcludeFilter  # noqa: E402
from berlin_flat_hunter.filters.wbs_filter import WbsFilter  # noqa: E402
from berlin_flat_hunter.filters.keywords import wbs_required  # noqa: E402
from berlin_flat_hunter.hunter import BerlinHunter  # noqa: E402


def _run(proc, exposes):
    return [e["url"] for e in proc.process_exposes(iter(exposes))]


def _ex(url, title, desc="", rooms="2 Zimmer", addr="Berlin"):
    return {"url": url, "title": title, "description": desc, "rooms": rooms, "address": addr}


# ── ExcludeFilter ────────────────────────────────────────────────────────
def test_default_shield_drops_junk_incl_description():
    f = ExcludeFilter(BerlinConfig({}))  # use_defaults True
    out = _run(f, [
        _ex("keep", "3-Zimmer-Wohnung"),
        _ex("senior", "Seniorenwohnung"),
        _ex("swap", "Schöne Wohnung", desc="Wohnungstausch gesucht"),  # tell only in description
    ])
    assert out == ["keep"]


def test_user_keyword_matches_description():
    f = ExcludeFilter(BerlinConfig({"exclude": {"keywords": ["western union"], "use_defaults": False}}))
    assert _run(f, [_ex("u", "Wohnung", desc="deposit via Western Union")]) == []


def test_furnished_title_guard_keeps_unmoebliert():
    f = ExcludeFilter(BerlinConfig({}))
    out = _run(f, [_ex("m", "Möblierte Wohnung"), _ex("un", "Unmöblierte Wohnung")])
    assert out == ["un"]


def test_parking_only_dropped_when_no_rooms():
    f = ExcludeFilter(BerlinConfig({}))
    out = _run(f, [_ex("flat", "Wohnung mit Stellplatz", rooms="3 Zimmer"),
                   _ex("garage", "Stellplatz", rooms="")])
    assert out == ["flat"]


def test_excluded_titles_fallback_gets_description_matching():
    # A YAML profile using flathunter excluded_titles now also matches description.
    f = ExcludeFilter(BerlinConfig({"excluded_titles": ["tausch"]}))
    assert _run(f, [_ex("u", "Wohnung", desc="biete Tauschwohnung")]) == []


# ── WbsFilter ────────────────────────────────────────────────────────────
def test_wbs_helper_negation_wins():
    assert wbs_required("WBS erforderlich") is True
    assert wbs_required("Wohnung ohne WBS") is False
    assert wbs_required("kein WBS benötigt") is False
    assert wbs_required("Schöne 2-Zimmer-Wohnung") is None


def test_wbs_filter_drops_required_keeps_negated_and_unmentioned():
    cfg = BerlinConfig({"filters": {"wbs_required": False}})
    assert WbsFilter.enabled_for(cfg)
    f = WbsFilter(cfg)
    out = _run(f, [
        _ex("req", "2-Zimmer-Wohnung (WBS erforderlich)"),
        _ex("neg", "Wohnung", desc="kein WBS benötigt"),
        _ex("none", "Schöne Wohnung"),
    ])
    assert out == ["neg", "none"]


def test_wbs_filter_inactive_when_has_wbs_or_unset():
    assert not WbsFilter.enabled_for(BerlinConfig({"filters": {"wbs_required": True}}))
    assert not WbsFilter.enabled_for(BerlinConfig({}))


# ── per-profile source gating ────────────────────────────────────────────
def test_source_gating_drops_unenabled_sources(tmp_path):
    db = str(tmp_path / "db.sqlite")
    os.makedirs(os.path.dirname(db), exist_ok=True)
    cfg = BerlinConfig({"database_location": db, "urls": ["https://www.wbm.de/x"]})
    cfg.init_searchers()
    h = BerlinHunter(cfg, IdMaintainer(db))
    try:
        assert h._enabled_sources == {"Wbm"}
        raw = [
            {"crawler": "Wbm", "id": 1, "title": "Wohnung A", "url": "https://www.wbm.de/a", "address": "Straße 1", "description": ""},
            {"crawler": "Gewobag", "id": 2, "title": "Wohnung B", "url": "https://www.gewobag.de/b", "address": "Straße 2", "description": ""},
            {"crawler": "Kleinanzeigen", "id": 3, "title": "Wohnung C", "url": "https://www.kleinanzeigen.de/c", "address": "Straße 3", "description": ""},
        ]
        titles = [e["title"] for e in h.process_raw(raw)]
        assert "Wohnung A" in titles
        assert "Wohnung B" not in titles and "Wohnung C" not in titles
    finally:
        h.close()
