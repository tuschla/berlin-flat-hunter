"""Backlog fixes: B3 dry-run retry, B10 manual dedup, B11 addy neg-cache,
B7/B8 send_mode+adapter, B5 imap automated, G6 prune."""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yaml  # noqa: E402

from berlin_flat_hunter.applicator import AutoApplicator  # noqa: E402
from berlin_flat_hunter.orchestrator import Orchestrator  # noqa: E402
from berlin_flat_hunter.config import BerlinConfig  # noqa: E402
from berlin_flat_hunter.email_alias.addy import AddyError  # noqa: E402
from berlin_flat_hunter.email_alias.resolver import AliasResolver  # noqa: E402
from berlin_flat_hunter.email_confirm.imap_reader import _is_automated  # noqa: E402
from berlin_flat_hunter.profile_json import userprofile_to_config  # noqa: E402
from berlin_flat_hunter.stats import StatsLogger  # noqa: E402
from berlin_flat_hunter.store import Store  # noqa: E402


# ── B3: failed dry-run retries ───────────────────────────────────────────
def test_has_send_ok_filter_lets_failed_dry_run_retry(tmp_path):
    s = Store(str(tmp_path / "s.db"))
    s.record_send("u", "k", mode="dry_run", channel="wbm-form", ok=False, recipient="a@x.de")
    assert s.has_send("u", "k", "dry_run", "a@x.de") is True           # a row exists
    assert s.has_send("u", "k", "dry_run", "a@x.de", ok=True) is False  # but no successful one


def _applicator(tmp_path, send_modes):
    s = Store(str(tmp_path / "s.db"))
    cfg = BerlinConfig({"auto_apply": {"enabled": True, "send_modes": send_modes},
                        "applicant": {"name": "Max", "email": "real@x.de"}})
    return AutoApplicator(cfg, store=s, user_id="u"), s


def test_already_sent_dry_run_needs_success(tmp_path):
    app, s = _applicator(tmp_path, {"wbm": "dry_run"})
    s.record_send("u", "k", mode="dry_run", channel="wbm-form", ok=False, recipient="a@x.de")
    assert app._already_sent("k", "a@x.de", "dry_run") is False  # failed -> retry
    s.record_send("u", "k", mode="dry_run", channel="wbm-form", ok=True, recipient="a@x.de")
    assert app._already_sent("k", "a@x.de", "dry_run") is True   # success -> dedup


# ── B10: manual-apply dedups listing-wide ────────────────────────────────
def test_manual_apply_suppresses_further_attempts(tmp_path):
    app, s = _applicator(tmp_path, {"gewobag": "live"})
    s.record_send("u", "g:1", mode="manual", channel="gewobag-form", ok=False, recipient="a@x.de")
    # Any address for that listing is now suppressed (site needs a human).
    assert app._already_sent("g:1", "b@x.de", "live") is True
    assert app._already_sent("g:1", "a@x.de", "live") is True


# ── B11: addy mint failure is negative-cached ────────────────────────────
def test_addy_mint_failure_not_retried_within_ttl(tmp_path, monkeypatch):
    s = Store(str(tmp_path / "s.db"))
    r = AliasResolver({"provider": "addy", "addy_api_key": "k", "granularity": "source"}, s, "u")
    calls = {"n": 0}

    class _FailClient:
        def __init__(self, *a, **k):
            pass

        def create_alias(self, **k):
            calls["n"] += 1
            raise AddyError("addy down")

    monkeypatch.setattr("berlin_flat_hunter.email_alias.resolver.AddyClient", _FailClient)
    assert r.emails_for("Howoge", "real@x.de", "k1") == ["real@x.de"]
    assert r.emails_for("Howoge", "real@x.de", "k2") == ["real@x.de"]  # same source bucket
    assert calls["n"] == 1  # second mint skipped (negative-cached)


# ── B7: explicit send_modes => unlisted sources off ──────────────────────
def test_send_mode_explicit_modes_make_unlisted_off():
    c = BerlinConfig({"auto_apply": {"enabled": True, "send_modes": {"gewobag": "live"}}})
    assert c.send_mode_for("gewobag") == "live"
    assert c.send_mode_for("kleinanzeigen") == "off"   # not listed -> off
    # legacy config with no send_modes keeps the global default
    c2 = BerlinConfig({"auto_apply": {"enabled": True, "dry_run": True}})
    assert c2.send_mode_for("anything") == "dry_run"


# ── B8: adapter forces disabled sources off ──────────────────────────────
def test_adapter_disabled_source_is_off():
    c = userprofile_to_config({"sources": {
        "gewobag": {"enabled": False, "send_mode": "live"},
        "howoge": {"enabled": True, "send_mode": "live"},
    }}, name="t", data_dir="/d")
    assert c["auto_apply"]["send_modes"]["gewobag"] == "off"
    assert c["auto_apply"]["send_modes"]["howoge"] == "live"


# ── B5: imap automated/newsletter suppression ────────────────────────────
def test_imap_is_automated():
    assert _is_automated("noreply@howoge.de", "Willkommen", "hi") is True
    assert _is_automated("newsletter@gewobag.de", "Neu", "hi") is True
    assert _is_automated("service@wbm.de", "Aktuelle Wohnungsangebote", "…") is True
    assert _is_automated("frau.meier@howoge.de", "Re: Ihre Anfrage", "gerne, Termin am …") is False


# ── G6: prune ────────────────────────────────────────────────────────────
def test_store_and_stats_prune(tmp_path):
    s = Store(str(tmp_path / "s.db"))
    old = int(time.time()) - 100 * 86400
    s._conn.execute("INSERT INTO sends(user_id,listing_key,ts,mode,channel,ok,message,recipient)"
                    " VALUES ('u','k',?,'live','x',1,'','a@x.de')", (old,))
    s._conn.commit()
    assert s.has_send("u", "k") is True
    removed = s.prune(60)
    assert removed.get("sends") == 1
    assert s.has_send("u", "k") is False
    assert s.prune(0) == {}  # opt-out no-op

    st = StatsLogger(str(tmp_path / "stats.db"))
    old_ts = time.time() - 100 * 86400
    st._conn.execute("INSERT INTO notices(id,crawler,url,first_seen_ts,last_seen_ts)"
                     " VALUES (1,'Wbm','u',?,?)", (old_ts, old_ts))
    st._conn.commit()
    assert st.prune(60) == 1
    assert st.prune(0) == 0


# ── B12: crawler-down alerts dedup per (bot, chat), union of chats ────────
def test_alert_channels_dedup_per_chat(tmp_path):
    db = str(tmp_path / "p1" / "db.sqlite")
    os.makedirs(os.path.dirname(db), exist_ok=True)
    prof = str(tmp_path / "p1.yaml")
    yaml.safe_dump({"database_location": db, "urls": ["https://www.wbm.de/x"],
                    "telegram": {"bot_token": "T", "receiver_ids": [1, 2]}}, open(prof, "w"))
    hy = str(tmp_path / "hunter.yaml")
    yaml.safe_dump({
        "global": {"database_location": str(tmp_path / "data" / "db.sqlite"),
                   "loop": {"active": False},
                   "telegram": {"bot_token": "T", "receiver_ids": [1]}},  # overlaps chat 1
        "profiles": [prof],
    }, open(hy, "w"))
    os.makedirs(str(tmp_path / "data"), exist_ok=True)
    o = Orchestrator.from_file(hy)
    try:
        chans = o.lead._alert_notifiers
        assert len(chans) == 1                      # single bot token T
        assert chans[0]._n.chat_ids == ["1", "2"]   # union; chat 1 not duplicated
    finally:
        o.close()
