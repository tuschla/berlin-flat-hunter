#!/usr/bin/env python3
"""Read-only URL probe for the crawler watchdog.

Fetches a URL from THIS host with plain ``requests`` and prints the HTTP status
plus key structural markers (listing container vs. captcha/block signals). It
exists to answer one question the triage otherwise cannot: *"is our crawler's
IP actually blocked?"*

The watchdog's WebFetch tool egresses through Anthropic's network, not this box,
so a WebFetch timeout says nothing about whether the crawler's own IP is being
served results — and a triage that guesses from WebFetch alone will misattribute
a headless-browser problem to a "datacenter-IP block". This probe fetches from
the box's real egress, so an ``HTTP 200`` with ``srchrslt-adtable`` present
proves the site serves us fine and points the finger at the browser/driver path.

Read-only by construction: one GET, prints markers, never writes. ``file://``
and other local schemes aren't supported by requests, so they just error out.

Usage: python scripts/probe_url.py <url>
"""
import sys

import requests

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

# Substrings worth reporting: listing containers (site is serving us results)
# and block/captcha tells (site is challenging us).
_MARKERS = (
    "srchrslt-adtable", "aditem",                 # Kleinanzeigen listings present
    "openimmo", "tx-openimmo",                    # WBM/TYPO3 listings present
    "awswaf-captcha", "initgeetest", "g-recaptcha",  # captcha systems
    "captcha", "datadome", "ich bin kein roboter",   # generic bot walls
    "access denied", "zugriff verweigert", "verify you are human",
)


def probe(url: str) -> str:
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=30)
    except requests.exceptions.RequestException as exc:
        return f"FETCH_ERROR: {type(exc).__name__}: {exc}"
    low = resp.text.lower()
    lines = [f"HTTP {resp.status_code}  size={len(resp.content)}  final={resp.url}"]
    for marker in _MARKERS:
        count = low.count(marker)
        if count:
            lines.append(f"  MARKER {marker}: {count}")
    if len(lines) == 1:
        lines.append("  (no known listing/captcha markers found)")
    return "\n".join(lines)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: probe_url.py <url>")
        return 2
    print(probe(sys.argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
