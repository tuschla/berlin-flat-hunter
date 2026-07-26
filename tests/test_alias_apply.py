"""AliasResolver + multi-email apply with per-recipient send dedup."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from berlin_flat_hunter.applicator import AutoApplicator  # noqa: E402
from berlin_flat_hunter.config import BerlinConfig  # noqa: E402
from berlin_flat_hunter.email_alias.addy import AddyError  # noqa: E402
from berlin_flat_hunter.email_alias.resolver import AliasResolver  # noqa: E402
from berlin_flat_hunter.store import Store  # noqa: E402


# ── AliasResolver ────────────────────────────────────────────────────────
def test_disabled_returns_chosen_or_real(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    r = AliasResolver({"provider": "none", "provider_emails": {"howoge": ["a@x.de"]}}, store, "single")
    assert r.emails_for("howoge", "real@x.de", "k") == ["a@x.de"]
    assert r.emails_for("wbm", "real@x.de", "k") == ["real@x.de"]


def test_addy_mints_and_caches_per_source(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "s.db"))
    r = AliasResolver({"provider": "addy", "addy_api_key": "key", "granularity": "source"}, store, "single")
    calls = {"n": 0}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def create_alias(self, **k):
            calls["n"] += 1
            return {"email": "alias1@addy.io", "id": "id1"}

    monkeypatch.setattr("berlin_flat_hunter.email_alias.resolver.AddyClient", FakeClient)
    assert r.emails_for("howoge", "real@x.de", "k1") == ["alias1@addy.io"]
    # Same source bucket on a different listing → cached, no second mint.
    assert r.emails_for("howoge", "real@x.de", "k2") == ["alias1@addy.io"]
    assert calls["n"] == 1


def test_provider_emails_case_insensitive_source(tmp_path):
    # Crawler names arrive capitalised (Howoge/Gewobag/Wbm) but provider_emails
    # is keyed lowercase — the alias pool must still be found.
    store = Store(str(tmp_path / "s.db"))
    r = AliasResolver({"provider": "addy", "addy_api_key": "",
                       "provider_emails": {"howoge": ["a@x.de", "b@x.de"]}}, store, "jakob")
    assert r.emails_for("Howoge", "real@x.de", "k") == ["a@x.de", "b@x.de"]
    assert r.emails_for("HOWOGE", "real@x.de", "k") == ["a@x.de", "b@x.de"]
    assert r.emails_for("howoge", "real@x.de", "k") == ["a@x.de", "b@x.de"]


def test_addy_failure_degrades_to_real(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "s.db"))
    r = AliasResolver({"provider": "addy", "addy_api_key": "key"}, store, "single")

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def create_alias(self, **k):
            raise AddyError("boom")

    monkeypatch.setattr("berlin_flat_hunter.email_alias.resolver.AddyClient", FakeClient)
    assert r.emails_for("howoge", "real@x.de", "k") == ["real@x.de"]


# ── multi-email apply via AutoApplicator ─────────────────────────────────
class _Spy:
    URL_MATCH = "example.com"
    SITE_NAME = "Gewobag"
    dry_run = False
    applicant: dict = {}

    def __init__(self):
        self.emails = []

    def apply(self, expose):
        self.emails.append(self.applicant.get("email"))
        return True

    def close(self):
        pass


class _Resolver:
    def emails_for(self, source, real, key):
        return ["a@x.de", "b@x.de"]


def test_applies_once_per_email_then_dedups(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    cfg = BerlinConfig({"auto_apply": {"enabled": True, "send_modes": {"gewobag": "live"}},
                        "applicant": {"name": "Max Mustermann", "email": "real@x.de"}})
    app = AutoApplicator(cfg, store=store, alias_resolver=_Resolver(), user_id="single")
    spy = _Spy()
    app.applicators = [spy]
    expose = {"url": "https://example.com/1", "crawler": "Gewobag", "id": "1"}

    app.process_expose(dict(expose))
    assert spy.emails == ["a@x.de", "b@x.de"]     # one live application per alias

    spy.emails.clear()
    app.process_expose(dict(expose))              # same listing next cycle
    assert spy.emails == []                        # both recipients already sent → skipped
