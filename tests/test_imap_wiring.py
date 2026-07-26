"""BerlinHunter IMAP glue: reply alerts (deduped) + disabled no-op."""
import os
import sys

import requests_mock as req_mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from flathunter.idmaintainer import IdMaintainer  # noqa: E402

from berlin_flat_hunter.config import BerlinConfig  # noqa: E402
from berlin_flat_hunter.hunter import BerlinHunter  # noqa: E402
import berlin_flat_hunter.email_confirm.imap_reader as imap_mod  # noqa: E402


def _hunter(tmp_path, imap_enabled):
    db = str(tmp_path / "single" / "db.sqlite")
    os.makedirs(os.path.dirname(db), exist_ok=True)
    cfg = BerlinConfig({
        "database_location": db,
        "telegram": {"bot_token": "TOK", "receiver_ids": ["555"]},
        "email_imap": {"enabled": imap_enabled, "host": "imap.x.de",
                       "username": "u@x.de", "password": "p"},
    })
    cfg.init_searchers()
    return BerlinHunter(cfg, IdMaintainer(cfg.database_location()))


class _FakeConfirmer:
    """Stand-in for ImapConfirmer: yields one landlord reply, no confirmations."""
    replies = [("mid-1", "vermieter@x.de", "Re: Wohnung", "Guten Tag ...")]

    def __init__(self, *a, **k):
        pass

    def scan(self):
        return ([], list(_FakeConfirmer.replies))


def test_reply_alert_sent_once_then_deduped(tmp_path, monkeypatch):
    h = _hunter(tmp_path, imap_enabled=True)
    monkeypatch.setattr(imap_mod, "ImapConfirmer", _FakeConfirmer)
    url = "https://api.telegram.org/botTOK/sendMessage"
    with req_mock.Mocker() as m:
        m.post(url, json={"ok": True}, status_code=200)
        h._run_imap_confirm()
        h._run_imap_confirm()  # same Message-ID → deduped, no second send
    assert len(m.request_history) == 1  # exactly one Telegram send across two scans
    assert "Vermieter" in m.request_history[0].text
    h.close()


def test_disabled_imap_is_noop(tmp_path, monkeypatch):
    h = _hunter(tmp_path, imap_enabled=False)
    called = {"n": 0}

    class Boom:
        def __init__(self, *a, **k):
            called["n"] += 1

        def scan(self):
            raise AssertionError("should not scan when disabled")

    monkeypatch.setattr(imap_mod, "ImapConfirmer", Boom)
    h._run_imap_confirm()  # must not construct/scan
    assert called["n"] == 0
    h.close()
