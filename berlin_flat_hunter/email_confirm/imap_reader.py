"""ImapConfirmer — auto-confirm Genossenschaft double-opt-in emails.

Several sources (notably Howoge) only register interest after the applicant
clicks a confirmation link in an email. The user brings their own inbox
(an IMAP config in their profile); we scan the last ``lookback_days`` for mails
from known Genossenschaft domains, extract opt-in/confirmation links, and GET
them to auto-confirm. It ALSO surfaces landlord replies.

Conservative by design: we only follow links whose host matches a known
Genossenschaft domain, never arbitrary URLs from the mailbox. Stdlib imaplib +
email + requests. Degrades to ([], []) on any error or when disabled.
"""

from __future__ import annotations

import email
import imaplib
import re
from email.header import decode_header, make_header
from email.message import Message
from collections.abc import Callable
from typing import Optional
from urllib.parse import urlparse

import requests

from flathunter.logging import logger

# Hosts we trust to follow confirmation links on. A link is only fetched if its
# netloc matches (or is a subdomain of) one of these.
_GENO_DOMAINS = (
    "howoge.de",
    "gewobag.de",
    "wbm.de",
    "degewo.de",
    "gesobau.de",
)

# Substrings that mark a link as a confirmation / double-opt-in link. Kept
# high-precision (only fetched on trusted Genossenschaft hosts) so we never
# auto-click an unsubscribe/abmelden link.
_CONFIRM_TOKENS = ("bestätig", "bestaetig", "confirm", "opt-in", "optin", "doi",
                   "verify", "aktivier", "freischalt", "double-opt")

# A mail with no confirm link is only surfaced as a landlord reply if it doesn't
# look automated. noreply senders + newsletter/marketing tells are dropped so
# Genossenschaft bulk mail doesn't masquerade as a personal reply.
_NOREPLY_RE = re.compile(
    r"(no[-_. ]?reply|noreply|newsletter|mailing|do[-_. ]?not[-_. ]?reply|"
    r"kein[-_. ]?antwort|donotreply)", re.IGNORECASE)
_NEWSLETTER_TELLS = (
    "newsletter", "wohnungsangebote", "mietangebote", "angebote der woche",
    "abmelden", "unsubscribe", "abbestellen", "jobangebote", "aktuelle angebote",
)


def _is_automated(sender: str, subject: str, body: str) -> bool:
    """True for no-reply / newsletter / marketing mail (not a landlord reply)."""
    if _NOREPLY_RE.search(sender or ""):
        return True
    low = (f"{subject} {body[:600]}").lower()
    return any(tell in low for tell in _NEWSLETTER_TELLS)

_HREF_RE = re.compile(r"""href=["']([^"']+)["']""", re.IGNORECASE)
_URL_RE = re.compile(r'''https?://[^\s"'<>)]+''', re.IGNORECASE)


def _decode(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _host_trusted(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(host == d or host.endswith("." + d) for d in _GENO_DOMAINS)


def _looks_like_confirm(url: str) -> bool:
    low = url.lower()
    return any(tok in low for tok in _CONFIRM_TOKENS)


def _body_text(msg: Message) -> str:
    """Concatenate text/plain + text/html parts as best-effort decoded text."""
    parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        parts.append(payload.decode(charset, errors="replace"))
                    except Exception:
                        parts.append(payload.decode("utf-8", errors="replace"))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            parts.append(payload.decode(charset, errors="replace"))
    return "\n".join(parts)


def _extract_confirm_links(body: str) -> list[str]:
    urls = set(_HREF_RE.findall(body)) | set(_URL_RE.findall(body))
    out = []
    for u in urls:
        u = u.strip()
        if _host_trusted(u) and _looks_like_confirm(u):
            out.append(u)
    return out


def _snippet(msg: Message, limit: int = 320) -> str:
    """A short, plain-text preview of the mail body for the alert."""
    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    text = payload.decode(charset, errors="replace")
                    break
    if not text:
        text = re.sub(r"<[^>]+>", " ", _body_text(msg))  # strip HTML tags
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


class ImapConfirmer:
    def __init__(
        self,
        imap_cfg: dict,
        *,
        timeout: float = 30.0,
        is_confirmed: Callable[[str], bool] | None = None,
        record_confirmation: Callable[[str, str, bool], None] | None = None,
    ) -> None:
        self.cfg = imap_cfg or {}
        self.timeout = timeout
        self.is_confirmed = is_confirmed or (lambda _url: False)
        self.record_confirmation = record_confirmation or (lambda _subject, _url, _ok: None)

    # --- config accessors (mirror the pi EmailImap dataclass defaults) ---
    @property
    def _enabled(self) -> bool:
        return bool(self.cfg.get("enabled"))

    @property
    def _host(self) -> str:
        return self.cfg.get("host") or ""

    @property
    def _port(self) -> int:
        return int(self.cfg.get("port", 993))

    @property
    def _username(self) -> str:
        return self.cfg.get("username") or ""

    @property
    def _password(self) -> str:
        return self.cfg.get("password") or ""

    @property
    def _use_ssl(self) -> bool:
        return bool(self.cfg.get("use_ssl", True))

    @property
    def _mailbox(self) -> str:
        return self.cfg.get("mailbox") or "INBOX"

    @property
    def _lookback_days(self) -> int:
        return int(self.cfg.get("lookback_days", 7))

    def scan(
        self,
    ) -> tuple[list[tuple[str, str, bool]], list[tuple[str, str, str, str]]]:
        """Scan the mailbox: auto-confirm opt-in links AND surface human replies.

        Returns ``(confirmations, replies)`` where confirmations are
        ``(subject, url, ok)`` and replies are ``(message_id, sender, subject,
        snippet)`` for landlord mails that are NOT double-opt-in confirmations
        (i.e. likely a real answer — viewing invite, acceptance, question).
        Both empty when disabled or on any connection error.
        """
        if not self._enabled or not self._host or not self._username or not self._password:
            return [], []
        try:
            return self._run()
        except Exception as exc:
            logger.warning("IMAP scan failed: %s", exc)
            return [], []

    def _run(self) -> tuple[list[tuple[str, str, bool]], list[tuple[str, str, str, str]]]:
        results: list[tuple[str, str, bool]] = []
        replies: list[tuple[str, str, str, str]] = []
        # Pass an explicit socket timeout so a stalled server can never wedge
        # the (single-threaded) runner loop: without it imaplib blocks forever
        # on read(). timeout= covers connect and every subsequent command.
        if self._use_ssl:
            conn = imaplib.IMAP4_SSL(self._host, self._port, timeout=self.timeout)
        else:
            conn = imaplib.IMAP4(self._host, self._port, timeout=self.timeout)
        try:
            conn.login(self._username, self._password)
            conn.select(self._mailbox)

            # Search by date window. IMAP SINCE wants a DD-Mon-YYYY date.
            import datetime

            since = (
                datetime.date.today()
                - datetime.timedelta(days=max(1, self._lookback_days))
            ).strftime("%d-%b-%Y")
            typ, data = conn.search(None, "SINCE", since)
            if typ != "OK" or not data or not data[0]:
                return results, replies
            ids = data[0].split()

            seen_urls: set[str] = set()
            for msg_id in ids:
                typ, raw = conn.fetch(msg_id, "(RFC822)")
                if typ != "OK" or not raw or not raw[0]:
                    continue
                msg = email.message_from_bytes(raw[0][1])
                sender = _decode(msg.get("From", ""))
                # Only consider mails from a known Genossenschaft domain.
                if not any(d in sender.lower() for d in _GENO_DOMAINS):
                    continue
                subject = _decode(msg.get("Subject", ""))
                body = _body_text(msg)
                links = _extract_confirm_links(body)
                if links:
                    # Double-opt-in mail: auto-confirm each link.
                    for url in links:
                        if url in seen_urls or self.is_confirmed(url):
                            continue
                        seen_urls.add(url)
                        ok = self._fetch(url)
                        if ok:
                            self.record_confirmation(subject, url, ok)
                        results.append((subject, url, ok))
                elif not _is_automated(sender, subject, body):
                    # No confirm link and not automated -> a real reply
                    # (viewing invite, answer). Newsletters/no-reply are skipped.
                    mid = (
                        _decode(msg.get("Message-ID", "")).strip()
                        or f"{sender.lower()}|{subject}"
                    )
                    replies.append((mid, sender, subject, _snippet(msg)))
            return results, replies
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def _fetch(self, url: str) -> bool:
        # Belt-and-braces: never GET a link outside the allowlist, even if a
        # caller reaches this directly.
        if not _host_trusted(url):
            logger.warning("IMAP confirm refused (untrusted host): %s", url)
            return False
        try:
            r = requests.get(url, timeout=self.timeout, allow_redirects=True)
            ok = r.status_code < 400
            logger.info("IMAP confirm GET %s -> HTTP %s", url, r.status_code)
            return ok
        except Exception as exc:
            logger.warning("IMAP confirm GET failed for %s: %s", url, exc)
            return False
