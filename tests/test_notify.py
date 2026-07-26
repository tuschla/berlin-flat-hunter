"""Tests for berlin_flat_hunter.notify — per-source Telegram routing."""
from __future__ import annotations

import re
from urllib.parse import parse_qs

import pytest

from berlin_flat_hunter.config import BerlinConfig
from berlin_flat_hunter.notify import BerlinNotifier, TelegramNotifier

SEND_MSG = re.compile(
    r"https://api\.telegram\.org/bot(?P<token>[^/]+)/sendMessage"
)


def _body(request):
    """Parse a form-encoded request body into a flat dict."""
    return {k: v[0] for k, v in parse_qs(request.text).items()}


def _expose(crawler="Gewobag", **kw):
    base = {
        "id": 1,
        "crawler": crawler,
        "title": "Nice flat",
        "rooms": "3 Zimmer",
        "size": "60 m²",
        "price": "600 €",
        "url": "https://example.com/flat/1",
        "address": "Berliner Str. 1",
    }
    base.update(kw)
    return base


def _config(**telegram):
    return BerlinConfig({"telegram": telegram})


# -- TelegramNotifier --------------------------------------------------------


def test_enabled_false_without_token():
    assert TelegramNotifier("", ["123"]).enabled is False


def test_enabled_false_without_chat():
    assert TelegramNotifier("tok", []).enabled is False


def test_enabled_true():
    assert TelegramNotifier("tok", ["123"]).enabled is True


def test_send_disabled_returns_false(requests_mock):
    assert TelegramNotifier("", []).send("hi") is False


def test_send_to_multiple_chats(requests_mock):
    m = requests_mock.post(SEND_MSG, json={"ok": True, "result": {}})
    ok = TelegramNotifier("tok", ["1", "2"]).send("hello")
    assert ok is True
    assert m.call_count == 2


def test_send_returns_false_on_error(requests_mock):
    requests_mock.post(SEND_MSG, status_code=400, json={"ok": False})
    assert TelegramNotifier("tok", ["1"]).send("hello") is False


def test_send_chunks_long_message(requests_mock):
    m = requests_mock.post(SEND_MSG, json={"ok": True, "result": {}})
    long = ("x" * 500 + "\n") * 20  # ~10k chars -> multiple chunks
    assert TelegramNotifier("tok", ["1"]).send(long) is True
    assert m.call_count > 1


# -- BerlinNotifier: default bot ---------------------------------------------


def test_default_bot_send(requests_mock):
    m = requests_mock.post(SEND_MSG, json={"ok": True, "result": {}})
    cfg = _config(bot_token="DEFTOK", receiver_ids=["999"])
    notifier = BerlinNotifier(cfg)
    list(notifier.process_exposes([_expose()]))
    assert m.call_count == 1
    assert _body(m.last_request)["chat_id"] == "999"
    # token in URL is the default token
    assert SEND_MSG.match(m.last_request.url).group("token") == "DEFTOK"


def test_message_contains_title_and_url(requests_mock):
    m = requests_mock.post(SEND_MSG, json={"ok": True, "result": {}})
    cfg = _config(bot_token="DEFTOK", receiver_ids=["999"])
    list(BerlinNotifier(cfg).process_exposes([_expose()]))
    body = _body(m.last_request)["text"]
    assert "Nice flat" in body
    assert "https://example.com/flat/1" in body


def test_sent_count_increments(requests_mock):
    requests_mock.post(SEND_MSG, json={"ok": True, "result": {}})
    cfg = _config(bot_token="DEFTOK", receiver_ids=["999"])
    notifier = BerlinNotifier(cfg)
    list(notifier.process_exposes([_expose(id=1), _expose(id=2)]))
    assert notifier.sent_count == 2


def test_sent_count_resets_each_cycle(requests_mock):
    requests_mock.post(SEND_MSG, json={"ok": True, "result": {}})
    cfg = _config(bot_token="DEFTOK", receiver_ids=["999"])
    notifier = BerlinNotifier(cfg)
    list(notifier.process_exposes([_expose()]))
    list(notifier.process_exposes([_expose()]))
    assert notifier.sent_count == 1


def test_exposes_pass_through(requests_mock):
    requests_mock.post(SEND_MSG, json={"ok": True, "result": {}})
    cfg = _config(bot_token="DEFTOK", receiver_ids=["999"])
    exposes = [_expose(id=1), _expose(id=2)]
    out = list(BerlinNotifier(cfg).process_exposes(exposes))
    assert [e["id"] for e in out] == [1, 2]


# -- BerlinNotifier: per-source routing --------------------------------------


def test_per_source_routing_to_different_bot(requests_mock):
    m = requests_mock.post(SEND_MSG, json={"ok": True, "result": {}})
    cfg = _config(
        bot_token="DEFTOK",
        receiver_ids=["999"],
        bots_by_source={"Kleinanzeigen": "KATOK"},
        chats_by_source={"Kleinanzeigen": ["555"]},
    )
    list(BerlinNotifier(cfg).process_exposes([_expose(crawler="Kleinanzeigen")]))
    assert SEND_MSG.match(m.last_request.url).group("token") == "KATOK"
    assert _body(m.last_request)["chat_id"] == "555"


def test_per_source_case_insensitive(requests_mock):
    m = requests_mock.post(SEND_MSG, json={"ok": True, "result": {}})
    cfg = _config(
        bot_token="DEFTOK",
        receiver_ids=["999"],
        bots_by_source={"kleinanzeigen": "KATOK"},
        chats_by_source={"kleinanzeigen": ["555"]},
    )
    list(BerlinNotifier(cfg).process_exposes([_expose(crawler="Kleinanzeigen")]))
    assert SEND_MSG.match(m.last_request.url).group("token") == "KATOK"


def test_fallback_when_source_has_no_override(requests_mock):
    m = requests_mock.post(SEND_MSG, json={"ok": True, "result": {}})
    cfg = _config(
        bot_token="DEFTOK",
        receiver_ids=["999"],
        bots_by_source={"Kleinanzeigen": "KATOK"},
        chats_by_source={"Kleinanzeigen": ["555"]},
    )
    # Gewobag has no override -> default bot + default chat.
    list(BerlinNotifier(cfg).process_exposes([_expose(crawler="Gewobag")]))
    assert SEND_MSG.match(m.last_request.url).group("token") == "DEFTOK"
    assert _body(m.last_request)["chat_id"] == "999"


def test_source_token_override_but_default_chats(requests_mock):
    """A source with a bot override but no chat override uses default chats."""
    m = requests_mock.post(SEND_MSG, json={"ok": True, "result": {}})
    cfg = _config(
        bot_token="DEFTOK",
        receiver_ids=["999"],
        bots_by_source={"Gewobag": "GEWTOK"},
    )
    list(BerlinNotifier(cfg).process_exposes([_expose(crawler="Gewobag")]))
    assert SEND_MSG.match(m.last_request.url).group("token") == "GEWTOK"
    assert _body(m.last_request)["chat_id"] == "999"


def test_notifier_cache_reuse(requests_mock):
    requests_mock.post(SEND_MSG, json={"ok": True, "result": {}})
    cfg = _config(bot_token="DEFTOK", receiver_ids=["999"])
    notifier = BerlinNotifier(cfg)
    list(notifier.process_exposes([_expose(id=1), _expose(id=2)]))
    # Both exposes route to the same (token, chats) -> one cached notifier.
    assert len(notifier._cache) == 1


# -- Heartbeat ---------------------------------------------------------------


def test_heartbeat_noop_without_log_channel(requests_mock):
    m = requests_mock.post(SEND_MSG, json={"ok": True, "result": {}})
    cfg = _config(bot_token="DEFTOK", receiver_ids=["999"])
    assert BerlinNotifier(cfg).send_heartbeat("cycle done") is False
    assert m.call_count == 0


def test_heartbeat_sends_with_log_channel(requests_mock):
    m = requests_mock.post(SEND_MSG, json={"ok": True, "result": {}})
    cfg = _config(
        bot_token="DEFTOK",
        receiver_ids=["999"],
        log_bot_token="LOGTOK",
        log_chat_id="777",
    )
    assert BerlinNotifier(cfg).send_heartbeat("cycle done") is True
    assert SEND_MSG.match(m.last_request.url).group("token") == "LOGTOK"
    assert _body(m.last_request)["chat_id"] == "777"
    assert _body(m.last_request)["text"] == "cycle done"


def test_heartbeat_falls_back_to_default_token(requests_mock):
    m = requests_mock.post(SEND_MSG, json={"ok": True, "result": {}})
    cfg = _config(
        bot_token="DEFTOK",
        receiver_ids=["999"],
        log_chat_id="777",  # log_bot_token omitted -> falls back to bot_token
    )
    assert BerlinNotifier(cfg).send_heartbeat("cycle done") is True
    assert SEND_MSG.match(m.last_request.url).group("token") == "DEFTOK"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
