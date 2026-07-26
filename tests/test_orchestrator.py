"""Shared-scrape orchestrator: crawl once, fan out to every profile."""
import os
import sys

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from berlin_flat_hunter.orchestrator import Orchestrator  # noqa: E402


def _write_profile(path, db, urls, extra=None):
    os.makedirs(os.path.dirname(db), exist_ok=True)
    cfg = {"database_location": db, "urls": urls}
    if extra:
        cfg.update(extra)
    with open(path, "w") as f:
        yaml.safe_dump(cfg, f)


def _build(tmp_path):
    single = str(tmp_path / "single.yaml")
    wg = str(tmp_path / "wg.yaml")
    _write_profile(single, str(tmp_path / "single" / "db.sqlite"),
                   ["https://www.wbm.de/x", "https://www.gewobag.de/y"],
                   {"filters": {"max_price": 600}})
    _write_profile(wg, str(tmp_path / "wg" / "db.sqlite"),
                   ["https://www.wbm.de/x", "https://www.howoge.de/z"])
    hunter_yaml = str(tmp_path / "hunter.yaml")
    with open(hunter_yaml, "w") as f:
        yaml.safe_dump({
            "global": {
                "database_location": str(tmp_path / "data" / "db.sqlite"),
                "loop": {"active": False},
                "urls": ["https://www.degewo.de/immosuche/"],
            },
            "profiles": [single, wg],
        }, f)
    os.makedirs(str(tmp_path / "data"), exist_ok=True)
    return Orchestrator.from_file(hunter_yaml)


def test_union_urls_dedup_and_extra(tmp_path):
    orch = _build(tmp_path)
    union = orch.lead.config.target_urls()
    # degewo (global extra) + both profiles' URLs, wbm/x appearing once.
    assert "https://www.degewo.de/immosuche/" in union
    assert union.count("https://www.wbm.de/x") == 1
    assert "https://www.gewobag.de/y" in union
    assert "https://www.howoge.de/z" in union


def test_run_once_dedups_and_fans_out(tmp_path):
    orch = _build(tmp_path)

    canned = [
        {"url": "a", "crawler": "Wbm", "id": 1, "title": "A"},
        {"url": "a", "crawler": "Wbm", "id": 1, "title": "A dup"},   # duplicate URL
        {"url": "b", "crawler": "Degewo", "id": 2, "title": "B"},
    ]
    orch.lead.crawl_for_exposes = lambda max_pages=None: iter(canned)

    health_ticks = []
    orch.lead._record_health = lambda raw: health_ticks.append([e["url"] for e in raw])

    received = {}
    for name, hunter in orch.profiles:
        hunter.process_raw = (lambda n: (lambda raw: received.__setitem__(
            n, [e["url"] for e in raw]) or []))(name)

    orch.run_once()

    # Health recorded exactly once, over the deduped pool.
    assert health_ticks == [["a", "b"]]
    # Every profile saw the same deduped pool.
    assert received["single"] == ["a", "b"]
    assert received["wg"] == ["a", "b"]


def test_one_bad_profile_does_not_sink_cycle(tmp_path):
    orch = _build(tmp_path)
    orch.lead.crawl_for_exposes = lambda max_pages=None: iter(
        [{"url": "a", "crawler": "Wbm", "id": 1, "title": "A"}])
    orch.lead._record_health = lambda raw: None

    ok = {}
    for i, (name, hunter) in enumerate(orch.profiles):
        if i == 0:
            hunter.process_raw = lambda raw: (_ for _ in ()).throw(RuntimeError("boom"))
        else:
            hunter.process_raw = (lambda n: (lambda raw: ok.__setitem__(n, True) or []))(name)

    orch.run_once()  # must not raise
    # The healthy profile still ran despite the first blowing up.
    assert any(ok.values())
