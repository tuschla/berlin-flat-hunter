"""Tests for the per-profile SQLite Store."""

from berlin_flat_hunter.store import Store


def _store(tmp_path):
    return Store(tmp_path / "sub" / "flathunt.sqlite")


def test_alias_round_trip(tmp_path):
    s = _store(tmp_path)
    assert s.get_alias("single", "howoge") is None
    s.save_alias("single", "howoge", "a@anon.me", alias_id="al-1")
    assert s.get_alias("single", "howoge") == "a@anon.me"
    # Upsert on the same (user, scope) replaces the address.
    s.save_alias("single", "howoge", "b@anon.me")
    assert s.get_alias("single", "howoge") == "b@anon.me"
    # Scoped by user + scope_key.
    assert s.get_alias("wg", "howoge") is None
    assert s.get_alias("single", "gewobag") is None
    s.close()


def test_creates_parent_dir(tmp_path):
    path = tmp_path / "a" / "b" / "c" / "db.sqlite"
    s = Store(path)
    assert path.exists()
    s.close()


def test_send_dedup_live_and_recipient(tmp_path):
    s = _store(tmp_path)
    lk = "Howoge:123"
    assert s.has_live_send("single", lk) is False

    # A dry_run does not count as a live send.
    s.record_send("single", lk, mode="dry_run", channel="howoge-form", ok=True,
                  recipient="landlord@x.de")
    assert s.has_live_send("single", lk) is False

    # A failed live send does not count.
    s.record_send("single", lk, mode="live", channel="howoge-form", ok=False,
                  recipient="landlord@x.de")
    assert s.has_live_send("single", lk) is False

    # A successful live send counts.
    s.record_send("single", lk, mode="live", channel="howoge-form", ok=True,
                  recipient="landlord@x.de")
    assert s.has_live_send("single", lk) is True
    # Per-recipient: a different recipient is its own application.
    assert s.has_live_send("single", lk, recipient="landlord@x.de") is True
    assert s.has_live_send("single", lk, recipient="other@x.de") is False
    s.close()


def test_has_send_mode_and_recipient(tmp_path):
    s = _store(tmp_path)
    lk = "Wbm:9"
    assert s.has_send("single", lk) is False
    s.record_send("single", lk, mode="dry_run", channel="wbm-form", ok=True,
                  recipient="r@x.de")
    assert s.has_send("single", lk) is True
    assert s.has_send("single", lk, mode="dry_run") is True
    assert s.has_send("single", lk, mode="live") is False
    assert s.has_send("single", lk, recipient="r@x.de") is True
    assert s.has_send("single", lk, recipient="nope@x.de") is False
    assert s.has_send("single", lk, mode="dry_run", recipient="r@x.de") is True
    # Different user is isolated.
    assert s.has_send("wg", lk) is False
    s.close()


def test_imap_confirmation_dedup(tmp_path):
    s = _store(tmp_path)
    url = "https://coop.de/confirm/token-abc"
    assert s.was_imap_confirmed("single", url) is False
    s.record_imap_confirmation("single", "Bitte bestätigen", url)
    assert s.was_imap_confirmed("single", url) is True
    # Re-recording is idempotent (no crash on PK conflict).
    s.record_imap_confirmation("single", "Bitte bestätigen", url)
    assert s.was_imap_confirmed("single", url) is True
    assert s.was_imap_confirmed("wg", url) is False
    s.close()


def test_email_notification_dedup(tmp_path):
    s = _store(tmp_path)
    mid = "<msg-123@mail.de>"
    assert s.was_email_notified("single", mid) is False
    s.record_email_notification("single", mid)
    assert s.was_email_notified("single", mid) is True
    # Idempotent re-record.
    s.record_email_notification("single", mid)
    assert s.was_email_notified("single", mid) is True
    assert s.was_email_notified("wg", mid) is False
    s.close()


def test_reopen_preserves_rows(tmp_path):
    path = tmp_path / "persist.sqlite"
    s = Store(path)
    s.save_alias("single", "fixed", "keep@anon.me", alias_id="al-x")
    s.record_send("single", "Howoge:1", mode="live", channel="howoge-form",
                  ok=True, recipient="r@x.de")
    s.record_imap_confirmation("single", "subj", "https://c/1")
    s.record_email_notification("single", "<m1@x>")
    s.close()

    s2 = Store(path)
    assert s2.get_alias("single", "fixed") == "keep@anon.me"
    assert s2.has_live_send("single", "Howoge:1", recipient="r@x.de") is True
    assert s2.was_imap_confirmed("single", "https://c/1") is True
    assert s2.was_email_notified("single", "<m1@x>") is True
    s2.close()
