"""Per-source send_mode: off/dry_run/live resolution + applicator gating."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from berlin_flat_hunter.applicator import AutoApplicator  # noqa: E402
from berlin_flat_hunter.config import BerlinConfig  # noqa: E402


def test_send_mode_for_precedence():
    c = BerlinConfig({"auto_apply": {"enabled": True, "dry_run": True,
                                     "send_modes": {"howoge": "live", "wbm": "off"}}})
    assert c.send_mode_for("howoge") == "live"
    assert c.send_mode_for("Howoge") == "live"          # case-insensitive
    assert c.send_mode_for("wbm") == "off"
    # With explicit per-source send_modes, an unlisted source is off (not the
    # global dry_run fallback) — so an unconfigured crawler never auto-applies.
    assert c.send_mode_for("gewobag") == "off"


def test_send_mode_global_live_and_disabled():
    assert BerlinConfig({"auto_apply": {"enabled": True, "dry_run": False}}).send_mode_for("x") == "live"
    assert BerlinConfig({}).send_mode_for("x") == "off"  # auto_apply not enabled


class _Spy:
    URL_MATCH = "example.com"
    SITE_NAME = "Spy"
    dry_run = False

    def __init__(self):
        self.calls = []

    def apply(self, expose):
        self.calls.append((expose.get("url"), self.dry_run))
        return False

    def close(self):
        pass


def _applicator(send_modes):
    cfg = BerlinConfig({"auto_apply": {"enabled": True, "dry_run": True, "send_modes": send_modes},
                        "applicant": {"name": "Max Mustermann", "email": "m@x.de"}})
    app = AutoApplicator(cfg)
    spy = _Spy()
    app.applicators = [spy]
    return app, spy


def test_off_source_is_not_applied():
    app, spy = _applicator({"kleinanzeigen": "off"})
    app.process_expose({"url": "https://example.com/1", "crawler": "Kleinanzeigen"})
    assert spy.calls == []  # off → never touched


def test_dry_run_and_live_flow_through_mode():
    app, spy = _applicator({"gewobag": "live", "wbm": "dry_run"})
    app.process_expose({"url": "https://example.com/live", "crawler": "Gewobag"})
    app.process_expose({"url": "https://example.com/dry", "crawler": "Wbm"})
    assert spy.calls == [("https://example.com/live", False),   # live => dry_run False
                         ("https://example.com/dry", True)]     # dry_run => True
