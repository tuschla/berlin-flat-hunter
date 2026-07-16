"""ScamFilter — keyword denylist for Kleinanzeigen listings.

Kleinanzeigen attracts a high share of advance-fee scams: rock-bottom rent in
prime areas, landlord "currently abroad" / "in another country", contact moves
to WhatsApp, deposit via Western Union, keys mailed without viewing. The
tells almost always appear in the *description body* (titles stay boring like
"3-Zimmer-Wohnung Neukölln"), so we scan title + address + description.

This filter drops exposes whose ``crawler == "Kleinanzeigen"`` (or whose URL
host contains ``kleinanzeigen.de``) when the combined haystack matches any
substring in a configurable denylist. Other crawlers pass through untouched —
the public-housing sites don't need this. The description snippet is captured
for free by ``berlin_flat_hunter.crawlers.kleinanzeigen.Kleinanzeigen`` off the
list page — no per-listing detail fetches.

Config shape (top-level)::

    scam_filter:
      enabled: true
      # extra patterns appended to the built-in denylist (case-insensitive
      # substrings; no regex by design — substring is robust to typos and
      # case variants without surprising the user)
      extra_patterns:
        - "deposit via"
        - "ich bin in london"

Disable a built-in pattern by setting ``override_patterns:`` to a full list
(replaces the defaults).
"""
import re
from typing import Iterable, Iterator, Optional
from urllib.parse import urlparse

from flathunter.abstract_processor import Processor
from flathunter.logging import logger


# Default denylist. Lower-case substrings (matched case-insensitively against
# title + address). Curated from the most common Berlin Kleinanzeigen scam
# templates seen 2024–2026. Add to this carefully — false positives quietly
# hide legitimate listings from the user.
DEFAULT_PATTERNS: tuple[str, ...] = (
    # "I'm currently abroad / moved abroad" advance-fee opener
    "auslandsumzug",
    "ins ausland",
    "im ausland",
    "lebe in",  # "lebe in London/Manchester/…"
    "wohne im ausland",
    "currently abroad",
    "moved abroad",
    "out of country",

    # Off-platform contact funnel
    "whatsapp",
    "telegram me",
    "viber",
    "kontakt per email",  # nudges off the platform's messaging
    "contact me at",

    # Money-rail tells
    "western union",
    "moneygram",
    "bitcoin",
    "kaution per post",
    "deposit by mail",
    "kaution überweisen sie",

    # "No viewing, keys by mail" core scam mechanic
    "ohne besichtigung",
    "schlüssel per post",
    "schluessel per post",
    "keys by mail",
    "keys by post",
    "no viewing required",

    # Religious / trust appeals (heavy correlation with scam templates)
    "god bless",
    "im namen gottes",
    "vertrauen sie mir",
    "trust me",

    # "Selling/letting urgently because…" sob-story openers
    "muss schnell weg",
    "dringend vermieten",
    "dringend verkaufen",
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class ScamFilter(Processor):
    """Drop kleinanzeigen exposes whose title/address matches a known scam pattern."""

    def __init__(self, config):
        cfg = config.scam_filter_config() if hasattr(config, "scam_filter_config") else {}
        override = cfg.get("override_patterns")
        if isinstance(override, list) and override:
            base: Iterable[str] = override
        else:
            base = DEFAULT_PATTERNS
        extra = cfg.get("extra_patterns") or []
        if not isinstance(extra, list):
            extra = []
        # Lowercase once at construction; matching is plain substring.
        patterns = [str(p).lower().strip() for p in list(base) + list(extra)]
        self._patterns: tuple[str, ...] = tuple(p for p in patterns if p)
        # Email-in-title is a strong scam signal but expressed as a regex,
        # not a substring; toggle independently in case a legitimate landlord
        # ever puts a contact email in the title (rare on Kleinanzeigen).
        self._flag_email = bool(cfg.get("flag_email_in_title", True))

    def process_exposes(self, exposes) -> Iterator[dict]:  # type: ignore[override]
        for expose in exposes:
            if not self._is_kleinanzeigen(expose):
                yield expose
                continue
            reason = self._scam_reason(expose)
            if reason is None:
                yield expose
                continue
            logger.info(
                "ScamFilter: dropping kleinanzeigen %s — matched %r",
                expose.get("url"), reason,
            )

    @staticmethod
    def _is_kleinanzeigen(expose: dict) -> bool:
        if (expose.get("crawler") or "").lower() == "kleinanzeigen":
            return True
        host = urlparse(expose.get("url", "")).netloc.lower()
        return "kleinanzeigen.de" in host

    def _scam_reason(self, expose: dict) -> Optional[str]:
        title = expose.get("title", "") or ""
        haystack = " ".join(filter(None, [
            title,
            expose.get("address", ""),
            expose.get("description", ""),
        ])).lower()
        if not haystack:
            return None
        for pattern in self._patterns:
            if pattern in haystack:
                return pattern
        if self._flag_email and _EMAIL_RE.search(title):
            return "email-in-title"
        # Description-body emails are also a strong scam signal (off-platform
        # contact funnel). The list-page description ships with the crawl, so
        # scanning it is free.
        if self._flag_email and _EMAIL_RE.search(expose.get("description", "") or ""):
            return "email-in-description"
        return None
