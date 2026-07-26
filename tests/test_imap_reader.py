"""Tests for the IMAP double-opt-in confirmer.

These never hit a real IMAP server or the network: ``imaplib.IMAP4_SSL`` /
``IMAP4`` are monkeypatched with a fake that serves canned messages, and
``requests.get`` (via the module's ``requests``) is intercepted.
"""

from __future__ import annotations

from email.message import EmailMessage

import pytest

from berlin_flat_hunter.email_confirm import imap_reader
from berlin_flat_hunter.email_confirm.imap_reader import ImapConfirmer


# --------------------------------------------------------------------------
# Helpers: build raw RFC822 mails and a fake imaplib client.
# --------------------------------------------------------------------------
def _make_mail(from_addr: str, subject: str, body: str,
               *, html: bool = False, message_id: str = "") -> bytes:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["Subject"] = subject
    if message_id:
        msg["Message-ID"] = message_id
    if html:
        msg.set_content("plain fallback")
        msg.add_alternative(body, subtype="html")
    else:
        msg.set_content(body)
    return msg.as_bytes()


class FakeIMAP:
    """Minimal imaplib-compatible stand-in.

    ``FakeIMAP.mails`` (a list of raw byte messages) is class-level state that
    each test sets before instantiation. Set ``FakeIMAP.raise_on_connect`` to an
    exception instance to simulate a connection failure.
    """

    mails: list[bytes] = []
    raise_on_connect = None
    last_search = None

    def __init__(self, host, port, timeout=None):
        if FakeIMAP.raise_on_connect is not None:
            raise FakeIMAP.raise_on_connect
        self.host = host
        self.port = port
        self.timeout = timeout
        # map of 1-based id (bytes) -> raw message
        self._by_id = {
            str(i + 1).encode(): raw for i, raw in enumerate(FakeIMAP.mails)
        }

    def login(self, username, password):
        return ("OK", [b"logged in"])

    def select(self, mailbox="INBOX"):
        return ("OK", [str(len(self._by_id)).encode()])

    def search(self, charset, *criteria):
        FakeIMAP.last_search = criteria
        return ("OK", [b" ".join(self._by_id.keys())])

    def fetch(self, msg_id, message_parts):
        raw = self._by_id.get(msg_id)
        if raw is None:
            return ("NO", [None])
        return ("OK", [(b"1 (RFC822 {%d}" % len(raw), raw)])

    def logout(self):
        return ("BYE", [b"bye"])


@pytest.fixture(autouse=True)
def _reset_fake():
    FakeIMAP.mails = []
    FakeIMAP.raise_on_connect = None
    FakeIMAP.last_search = None
    yield
    FakeIMAP.mails = []
    FakeIMAP.raise_on_connect = None


@pytest.fixture
def patch_imap(monkeypatch):
    monkeypatch.setattr(imap_reader.imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setattr(imap_reader.imaplib, "IMAP4", FakeIMAP)
    return FakeIMAP


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


@pytest.fixture
def capture_get(monkeypatch):
    calls: list[str] = []

    def fake_get(url, timeout=None, allow_redirects=True):
        calls.append(url)
        return FakeResponse(200)

    monkeypatch.setattr(imap_reader.requests, "get", fake_get)
    return calls


def _cfg(**over):
    base = {
        "enabled": True,
        "host": "imap.example.com",
        "username": "u@example.com",
        "password": "secret",
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------
def test_disabled_config_returns_empty(patch_imap, capture_get):
    confirmer = ImapConfirmer(_cfg(enabled=False))
    assert confirmer.scan() == ([], [])
    assert capture_get == []


def test_missing_credentials_returns_empty(patch_imap, capture_get):
    confirmer = ImapConfirmer(_cfg(username="", password=""))
    assert confirmer.scan() == ([], [])
    assert capture_get == []


def test_howoge_confirm_link_is_fetched_and_recorded(patch_imap, capture_get):
    url = "https://www.howoge.de/bestaetigen?token=abc123"
    FakeIMAP.mails = [
        _make_mail(
            "Howoge <noreply@howoge.de>",
            "Bitte bestätigen Sie Ihre Anmeldung",
            f'<a href="{url}">Jetzt bestätigen</a>',
            html=True,
        )
    ]
    recorded: list[tuple] = []
    confirmer = ImapConfirmer(
        _cfg(),
        record_confirmation=lambda s, u, ok: recorded.append((s, u, ok)),
    )
    confirmations, replies = confirmer.scan()

    assert capture_get == [url]
    assert len(confirmations) == 1
    subject, got_url, ok = confirmations[0]
    assert got_url == url
    assert ok is True
    assert replies == []
    assert recorded == [(subject, url, True)]


def test_non_allowlisted_host_not_fetched(patch_imap, capture_get):
    # Confirm-looking link, but a Genossenschaft sender, host NOT on allowlist.
    evil = "https://evil.example.net/confirm?token=xyz"
    FakeIMAP.mails = [
        _make_mail(
            "Howoge <noreply@howoge.de>",
            "Bitte bestätigen",
            f'<a href="{evil}">confirm</a>',
            html=True,
        )
    ]
    confirmer = ImapConfirmer(_cfg())
    confirmations, replies = confirmer.scan()

    assert capture_get == []
    assert confirmations == []
    # No confirm link extracted -> treated as a reply.
    assert len(replies) == 1


def test_is_confirmed_skips_refetch(patch_imap, capture_get):
    url = "https://www.howoge.de/optin?token=dup"
    FakeIMAP.mails = [
        _make_mail(
            "Howoge <noreply@howoge.de>",
            "Anmeldung bestätigen",
            f'<a href="{url}">optin</a>',
            html=True,
        )
    ]
    confirmer = ImapConfirmer(_cfg(), is_confirmed=lambda u: True)
    confirmations, replies = confirmer.scan()

    assert capture_get == []  # already confirmed -> not fetched again
    assert confirmations == []
    assert replies == []


def test_connection_error_returns_empty(patch_imap, capture_get):
    FakeIMAP.raise_on_connect = OSError("connection refused")
    confirmer = ImapConfirmer(_cfg())
    assert confirmer.scan() == ([], [])
    assert capture_get == []


def test_landlord_reply_surfaces(patch_imap, capture_get):
    FakeIMAP.mails = [
        _make_mail(
            "Vermietung <vermietung@gewobag.de>",
            "Ihre Wohnungsanfrage - Besichtigungstermin",
            "Guten Tag, wir laden Sie zur Besichtigung am Montag ein.",
            message_id="<reply-1@gewobag.de>",
        )
    ]
    confirmer = ImapConfirmer(_cfg())
    confirmations, replies = confirmer.scan()

    assert confirmations == []
    assert capture_get == []
    assert len(replies) == 1
    mid, sender, subject, snippet = replies[0]
    assert mid == "<reply-1@gewobag.de>"
    assert "gewobag.de" in sender
    assert "Besichtigungstermin" in subject
    assert "Besichtigung" in snippet


def test_non_geno_sender_ignored(patch_imap, capture_get):
    FakeIMAP.mails = [
        _make_mail(
            "Random <someone@gmail.com>",
            "Bitte bestätigen",
            '<a href="https://www.howoge.de/confirm?t=1">confirm</a>',
            html=True,
        )
    ]
    confirmer = ImapConfirmer(_cfg())
    confirmations, replies = confirmer.scan()
    assert (confirmations, replies) == ([], [])
    assert capture_get == []
