"""Review-fix regressions: Telegram retry/chunking, KA multi-email guard,
WBM form_answers mapping."""
import os
import sys

import requests_mock as req_mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from berlin_flat_hunter.applicator import AutoApplicator, _wbm_answer_fields  # noqa: E402
from berlin_flat_hunter.config import BerlinConfig  # noqa: E402
from berlin_flat_hunter.notify import TelegramNotifier  # noqa: E402
from berlin_flat_hunter.store import Store  # noqa: E402


# ── B1: Telegram retry + B9: chunk hard-split ────────────────────────────
def test_send_retries_transient_then_succeeds(monkeypatch):
    monkeypatch.setattr("berlin_flat_hunter.notify.time.sleep", lambda *_: None)
    n = TelegramNotifier("TOK", ["1"], timeout=1)
    url = "https://api.telegram.org/botTOK/sendMessage"
    with req_mock.Mocker() as m:
        m.post(url, [
            {"status_code": 429, "json": {"parameters": {"retry_after": 0}}},
            {"status_code": 500, "text": "err"},
            {"status_code": 200, "json": {"ok": True}},
        ])
        assert n.send("hi") is True
        assert m.call_count == 3


def test_send_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr("berlin_flat_hunter.notify.time.sleep", lambda *_: None)
    n = TelegramNotifier("TOK", ["1"], timeout=1)
    url = "https://api.telegram.org/botTOK/sendMessage"
    with req_mock.Mocker() as m:
        m.post(url, status_code=500, text="down")
        assert n.send("hi") is False
        assert m.call_count == 3  # bounded retries


def test_chunks_hard_split_long_line():
    n = TelegramNotifier("T", ["1"])
    long_line = "x" * 9000  # single line, no newlines, > MAX_LEN
    chunks = n._chunks(long_line)
    assert all(len(c) <= n.MAX_LEN for c in chunks)
    assert "".join(chunks) == long_line


# ── B2: account-based site applies once despite multiple aliases ─────────
class _KaSpy:
    URL_MATCH = "kleinanzeigen.de"
    SITE_NAME = "Kleinanzeigen"
    ACCOUNT_BASED = True
    dry_run = False
    applicant: dict = {}

    def __init__(self):
        self.emails = []

    def apply(self, expose):
        self.emails.append(self.applicant.get("email"))
        return True

    def close(self):
        pass


class _ThreeAliases:
    def emails_for(self, source, real, key):
        return ["a@x.de", "b@x.de", "c@x.de"]


def test_kleinanzeigen_applies_once_despite_aliases(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    cfg = BerlinConfig({"auto_apply": {"enabled": True, "send_modes": {"kleinanzeigen": "live"}},
                        "applicant": {"name": "Max", "email": "real@x.de"}})
    app = AutoApplicator(cfg, store=store, alias_resolver=_ThreeAliases(), user_id="u")
    spy = _KaSpy()
    app.applicators = [spy]
    app.process_expose({"url": "https://www.kleinanzeigen.de/1", "crawler": "Kleinanzeigen", "id": "1"})
    assert len(spy.emails) == 1  # once, not 3x


# ── G5: WBM form_answers → powermail field names ─────────────────────────
def test_wbm_answer_fields_maps_salutation():
    out = _wbm_answer_fields({"email": "x@y.de"}, {"salutation": "Frau"})
    assert out.get("anrede") == "Frau"


def test_wbm_answer_fields_empty_without_answers():
    assert _wbm_answer_fields({"email": "x"}, {}) == {}
