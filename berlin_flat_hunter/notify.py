"""Per-source Telegram notification for the Berlin flat hunter.

This module provides a drop-in replacement for flathunter's built-in
``SenderTelegram`` processor. Instead of broadcasting every listing to a single
bot + chat list, :class:`BerlinNotifier` routes each listing to a bot/chat that
belongs to the listing's *source* (crawler), so notifications from e.g. Gewobag
and Kleinanzeigen can land in different Telegram channels.

- :class:`TelegramNotifier` is a minimal Telegram Bot API client (ported from
  ``pi_flathuntbot/server/flathunt_server/notifier.py`` but using ``requests``
  instead of ``httpx``). It fans a single message out to one or more chat ids
  and chunks long messages.
- :class:`BerlinNotifier` is the flathunter ``Processor`` that formats each
  expose with the same template flathunter uses and routes it to the right bot.
  It also offers :meth:`BerlinNotifier.send_heartbeat` for a per-cycle status
  message to an optional log channel.

Config schema (all under the top-level ``telegram:`` block)::

    telegram:
      bot_token: "<default bot token>"          # existing key
      receiver_ids: ["<chat id>", ...]           # existing key
      bots_by_source:                            # optional per-source override
        Gewobag: "<gewobag bot token>"
        Kleinanzeigen: "<ka bot token>"
      chats_by_source:                           # optional per-source override
        Gewobag: ["<chat id>", ...]
        Kleinanzeigen: ["<chat id>", ...]
      log_bot_token: "<heartbeat bot token>"     # optional; falls back to bot_token
      log_chat_id: "<heartbeat chat id>"         # optional; str or list of str

With only ``bot_token`` + ``receiver_ids`` configured, :class:`BerlinNotifier`
behaves exactly like today's single-bot notifier.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Iterable, List, Optional, Tuple

import requests

from flathunter.abstract_processor import Processor

log = logging.getLogger(__name__)

# Retry transient Telegram failures so a 429/5xx/timeout doesn't permanently
# drop a matched listing (the expose is already marked "seen" upstream, so a
# lost send is never retried on a later cycle).
_SEND_ATTEMPTS = 3
_BACKOFF_SECONDS = (1.0, 3.0)   # waited before attempts 2 and 3
_RETRY_AFTER_CAP = 30.0         # cap a 429 Retry-After so we never block a cycle for minutes


class TelegramNotifier:
    """Minimal Telegram Bot API client.

    Sends a text message to every configured chat id, chunking messages that
    exceed Telegram's length cap. Never raises: send failures are logged and
    surface as a ``False`` return value.
    """

    API = "https://api.telegram.org"
    MAX_LEN = 4000  # Telegram hard cap is 4096; leave headroom.

    def __init__(
        self,
        bot_token: str = "",
        chat_ids: Optional[Iterable[str]] = None,
        *,
        timeout: float = 30.0,
    ) -> None:
        self.bot_token = bot_token or ""
        # Normalise chat ids to a list of strings.
        self.chat_ids: List[str] = [str(c) for c in (chat_ids or []) if str(c)]
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        """True only if we have a token and at least one chat id."""
        return bool(self.bot_token and self.chat_ids)

    def send(self, text: str, *, parse_mode: Optional[str] = None) -> bool:
        """Send ``text`` (chunked) to every chat id.

        Returns True only if every chunk reached every chat. Never raises.
        """
        if not self.enabled:
            log.warning(
                "Telegram disabled (missing token or chat id) - would send:\n%s", text
            )
            return False
        url = f"{self.API}/bot{self.bot_token}/sendMessage"
        chunks = self._chunks(text)
        ok = True
        for chat_id in self.chat_ids:
            for chunk in chunks:
                payload = {
                    "chat_id": str(chat_id),
                    "text": chunk,
                    "disable_web_page_preview": False,
                }
                if parse_mode:
                    payload["parse_mode"] = parse_mode
                if not self._post_with_retry(url, payload):
                    ok = False
        return ok

    def _post_with_retry(self, url: str, payload: dict) -> bool:
        """POST one chunk, retrying transient failures (network / 429 / 5xx).
        Returns True on a 200, False after exhausting attempts. Never raises."""
        for attempt in range(_SEND_ATTEMPTS):
            last = attempt == _SEND_ATTEMPTS - 1
            try:
                r = requests.post(url, data=payload, timeout=self.timeout)
            except Exception as exc:  # noqa: BLE001 - never raise to caller
                log.warning("Telegram send error (attempt %d/%d): %s",
                            attempt + 1, _SEND_ATTEMPTS, exc)
                if last:
                    return False
                time.sleep(_BACKOFF_SECONDS[attempt])
                continue
            if r.status_code == 200:
                return True
            if r.status_code == 429 and not last:
                time.sleep(self._retry_after(r, _BACKOFF_SECONDS[attempt]))
                continue
            if 500 <= r.status_code < 600 and not last:
                log.warning("Telegram %s (attempt %d/%d), retrying",
                            r.status_code, attempt + 1, _SEND_ATTEMPTS)
                time.sleep(_BACKOFF_SECONDS[attempt])
                continue
            log.error("Telegram send failed (%s): %s", r.status_code, r.text)
            return False
        return False

    @staticmethod
    def _retry_after(resp, fallback: float) -> float:
        """Seconds to wait for a 429, from Telegram's parameters.retry_after
        (capped), else the backoff fallback."""
        try:
            ra = float(resp.json().get("parameters", {}).get("retry_after", 0))
        except Exception:  # noqa: BLE001
            ra = 0.0
        return min(ra, _RETRY_AFTER_CAP) if ra > 0 else fallback

    def _chunks(self, text: str) -> List[str]:
        if len(text) <= self.MAX_LEN:
            return [text]
        out: List[str] = []
        buf = ""
        for line in text.splitlines(keepends=True):
            # A single line longer than the cap must be hard-split, else it would
            # be emitted as an over-cap chunk that Telegram rejects with a 400.
            while len(line) > self.MAX_LEN:
                if buf:
                    out.append(buf)
                    buf = ""
                out.append(line[:self.MAX_LEN])
                line = line[self.MAX_LEN:]
            if len(buf) + len(line) > self.MAX_LEN:
                if buf:
                    out.append(buf)
                buf = line
            else:
                buf += line
        if buf:
            out.append(buf)
        return out


class BerlinNotifier(Processor):
    """Expose processor that routes each listing to its source's Telegram bot.

    Drop-in replacement for flathunter's ``SenderTelegram`` in our per-profile
    pipeline. For each expose it formats a message with the same template
    flathunter uses (``config.message_format()`` + the same field substitution
    as ``SenderTelegram.__get_text_message``) and sends it to the bot/chat that
    belongs to the expose's ``crawler``, falling back to the default bot.
    """

    def __init__(self, config) -> None:
        self.config = config
        self.message_format = config.message_format()

        telegram = config.get("telegram", {}) or {}
        self._bots_by_source: Dict[str, str] = telegram.get("bots_by_source", {}) or {}
        self._chats_by_source: Dict[str, List[str]] = (
            telegram.get("chats_by_source", {}) or {}
        )

        # Default bot fallback (backward compatible single-bot behaviour).
        self._default_token: str = config.telegram_bot_token() or ""
        self._default_chats: List[str] = [
            str(c) for c in (config.telegram_receiver_ids() or [])
        ]

        # Heartbeat / log channel (optional).
        self._log_token: str = telegram.get("log_bot_token") or self._default_token
        log_chat = telegram.get("log_chat_id")
        if log_chat is None:
            self._log_chats: List[str] = []
        elif isinstance(log_chat, (list, tuple)):
            self._log_chats = [str(c) for c in log_chat if str(c)]
        else:
            self._log_chats = [str(log_chat)] if str(log_chat) else []

        # Cache TelegramNotifier per (token, tuple(chat_ids)).
        self._cache: Dict[Tuple[str, Tuple[str, ...]], TelegramNotifier] = {}

        # Number of notifications successfully sent this cycle.
        self.sent_count: int = 0

    # -- notifier caching --------------------------------------------------

    def _notifier_for(self, token: str, chat_ids: List[str]) -> TelegramNotifier:
        key = (token or "", tuple(str(c) for c in chat_ids))
        notifier = self._cache.get(key)
        if notifier is None:
            notifier = TelegramNotifier(token, chat_ids)
            self._cache[key] = notifier
        return notifier

    @staticmethod
    def _lookup(mapping: Dict, crawler: str):
        """Case-insensitive-friendly lookup: exact, then lowercased key."""
        if crawler in mapping:
            return mapping[crawler]
        lowered = crawler.lower()
        if lowered in mapping:
            return mapping[lowered]
        for key, value in mapping.items():
            if str(key).lower() == lowered:
                return value
        return None

    def _route(self, crawler: str) -> Tuple[str, List[str]]:
        """Return (token, chat_ids) for the given crawler name."""
        token = self._lookup(self._bots_by_source, crawler)
        chats = self._lookup(self._chats_by_source, crawler)
        if token is None:
            token = self._default_token
        if chats is None:
            chats = self._default_chats
        if isinstance(chats, (str, int)):
            chats = [str(chats)]
        return token or "", [str(c) for c in (chats or [])]

    # -- message formatting (mirrors SenderTelegram.__get_text_message) -----

    def _get_text_message(self, expose: Dict) -> str:
        return self.message_format.format(
            crawler=expose.get("crawler", "N/A"),
            title=expose.get("title", "N/A"),
            rooms=expose.get("rooms", "N/A"),
            size=expose.get("size", "N/A"),
            price=expose.get("price", "N/A"),
            url=expose.get("url", "N/A"),
            address=expose.get("address", "N/A"),
            durations=expose.get("durations", "N/A"),
        ).strip()

    # -- Processor interface -----------------------------------------------

    def process_exposes(self, exposes):
        """Send a notification for each expose, then pass it through.

        Resets :attr:`sent_count` at the start of each cycle so the caller can
        build a heartbeat afterwards.
        """
        self.sent_count = 0
        for expose in exposes:
            crawler = str(expose.get("crawler", "") or "")
            token, chats = self._route(crawler)
            notifier = self._notifier_for(token, chats)
            message = self._get_text_message(expose)
            try:
                if notifier.send(message):
                    self.sent_count += 1
            except Exception as exc:  # noqa: BLE001 - never break the pipeline
                log.error("Notification failed for expose %s: %s",
                          expose.get("id"), exc)
            # Always pass the expose downstream (auto-applicator, etc.).
            yield expose

    def process_expose(self, expose: Dict) -> Dict:
        """Send a notification for a single expose and return it unchanged."""
        crawler = str(expose.get("crawler", "") or "")
        token, chats = self._route(crawler)
        notifier = self._notifier_for(token, chats)
        try:
            if notifier.send(self._get_text_message(expose)):
                self.sent_count += 1
        except Exception as exc:  # noqa: BLE001
            log.error("Notification failed for expose %s: %s",
                      expose.get("id"), exc)
        return expose

    # -- heartbeat ---------------------------------------------------------

    def send_heartbeat(self, summary_text: str) -> bool:
        """Send a per-cycle heartbeat to the log channel, if configured.

        Returns False (no-op) if no log channel is configured.
        """
        if not (self._log_token and self._log_chats):
            return False
        notifier = self._notifier_for(self._log_token, self._log_chats)
        return notifier.send(summary_text)
