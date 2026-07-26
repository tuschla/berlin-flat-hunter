"""Tests for the addy.io client — urllib.request.urlopen is monkeypatched.

addy.py speaks HTTP via the standard library (not requests), so we simulate the
network at the ``urllib.request.urlopen`` boundary: a fake response object for
success cases, and ``urllib.error.HTTPError`` for auth/other failures.
"""

import io
import json
import urllib.error
from unittest import mock

import pytest

from berlin_flat_hunter.email_alias.addy import AddyClient, AddyError


class _FakeResp:
    """Context-manager stand-in for the urlopen() response."""

    def __init__(self, body):
        self._body = body.encode("utf-8") if isinstance(body, str) else body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code):
    return urllib.error.HTTPError(
        url="https://app.addy.io/api/v1/account-details",
        code=code,
        msg="Unauthorized" if code == 401 else "Error",
        hdrs=None,
        fp=io.BytesIO(b'{"message":"nope"}'),
    )


def _client():
    return AddyClient("sk-test-key", base_url="https://app.addy.io")


def test_account_details_success():
    payload = {"data": {"username": "leon", "id": "acc-1"}}
    with mock.patch("urllib.request.urlopen", return_value=_FakeResp(json.dumps(payload))):
        data = _client().account_details()
    assert data == {"username": "leon", "id": "acc-1"}


def test_test_returns_true_on_success():
    payload = {"data": {"username": "leon"}}
    with mock.patch("urllib.request.urlopen", return_value=_FakeResp(json.dumps(payload))):
        ok, msg = _client().test()
    assert ok is True
    assert "leon" in msg


def test_test_returns_false_on_auth_failure():
    with mock.patch("urllib.request.urlopen", side_effect=_http_error(401)):
        ok, msg = _client().test()
    assert ok is False
    assert "key" in msg.lower()


def test_request_raises_addyerror_on_401():
    with mock.patch("urllib.request.urlopen", side_effect=_http_error(401)):
        with pytest.raises(AddyError):
            _client().account_details()


def test_no_api_key_raises():
    with pytest.raises(AddyError):
        AddyClient("").account_details()


def test_create_alias_posts_and_returns_email():
    """create_alias resolves defaults, POSTs, and returns the alias record."""
    domain_opts = {"data": ["anonaddy.me"], "defaultAliasDomain": "anonaddy.me",
                   "defaultAliasFormat": "random_characters"}
    created = {"data": {"email": "abc123@anonaddy.me", "id": "alias-9"}}

    calls = []

    def fake_urlopen(req, timeout=None):
        # Record method + url + body so we can assert the POST happened.
        body = req.data.decode("utf-8") if req.data else None
        calls.append((req.get_method(), req.full_url, body))
        if req.get_method() == "GET":
            return _FakeResp(json.dumps(domain_opts))
        return _FakeResp(json.dumps(created))

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        rec = _client().create_alias(description="howoge")

    assert rec["email"] == "abc123@anonaddy.me"
    methods = [c[0] for c in calls]
    assert "POST" in methods
    # The POST body carries the resolved domain + format + description.
    post = next(c for c in calls if c[0] == "POST")
    sent = json.loads(post[2])
    assert sent["domain"] == "anonaddy.me"
    assert sent["format"] == "random_characters"
    assert sent["description"] == "howoge"


def test_resolve_defaults_degrades_gracefully():
    """If domain-options errors, resolve_defaults returns a safe fallback."""
    with mock.patch("urllib.request.urlopen", side_effect=_http_error(500)):
        domain, fmt = _client().resolve_defaults()
    assert domain == ""
    assert fmt == "random_characters"


def test_resolve_defaults_from_options():
    opts = {"data": ["one.me", "two.me"], "defaultAliasDomain": "one.me",
            "defaultAliasFormat": "uuid"}
    with mock.patch("urllib.request.urlopen", return_value=_FakeResp(json.dumps(opts))):
        domain, fmt = _client().resolve_defaults()
    assert domain == "one.me"
    assert fmt == "uuid"
