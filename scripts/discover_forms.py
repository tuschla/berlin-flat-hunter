#!/usr/bin/env python3
"""Form-field discovery tool for the Berlin Genossenschaft application forms.

Scrapes the *live* application forms of the auto-send providers (Howoge, WBM and
Gewobag) and enumerates every visible input field, so the unified questionnaire
catalog in ``berlin_flat_hunter/forms/catalog.py`` can be kept in sync when a
site changes its form.

Two classes of form:

* **howoge / wbm** — plain server-rendered HTML. We reach the form with
  ``requests`` + ``BeautifulSoup`` (lxml) exactly the way the live crawlers in
  ``berlin_flat_hunter/crawlers/`` do, then enumerate fields.
* **gewobag** — the listing page contains an ``<iframe id="contact-iframe">``
  whose ``src`` is a wohnungshelden Angular SPA. The iframe URL sits in the
  *static* HTML (regex-able without a browser), but the form fields inside it
  only exist after JavaScript runs, so we load that URL with **Playwright**
  (headless Chromium) and read ``input/select/textarea[formcontrolname]``.

The script never crashes because one provider fails: errors are collected per
provider and reported at the end. Gewobag degrades gracefully when Playwright or
its Chromium browser is unavailable (e.g. on a Raspberry Pi or in a sandbox).

Output: a merged JSON document (same shape as the existing
``berlin_flat_hunter/forms/discovered_raw.json``) plus a human-readable summary
table and a suggested ``catalog.py`` mapping block for Gewobag.

This is an offline maintenance tool — it is NOT part of the runtime app and does
NOT run in CI.

Usage examples::

    python scripts/discover_forms.py                       # all three, default out
    python scripts/discover_forms.py --providers howoge,wbm # HTTP only
    python scripts/discover_forms.py --no-browser           # skip gewobag/Playwright
    python scripts/discover_forms.py --providers gewobag --listing <url>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

# --------------------------------------------------------------------------- #
# Path / repo setup
# --------------------------------------------------------------------------- #
# This file lives at <repo>/scripts/discover_forms.py.
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "berlin_flat_hunter" / "forms" / "discovered_raw.json"

# Make the berlin_flat_hunter package importable even when the caller runs the
# script from outside an installed environment. Prepend so the in-repo copy wins.
_repo_str = str(REPO_ROOT)
if _repo_str not in sys.path:
    sys.path.insert(0, _repo_str)

ALL_PROVIDERS = ("howoge", "wbm", "gewobag")

# Set True when the user forces a specific --listing, so gewobag discovery won't
# silently scan for a different (working) listing behind their back.
_LISTING_OVERRIDDEN = False
HTTP_PROVIDERS = ("howoge", "wbm")          # reachable without a browser
BROWSER_PROVIDERS = ("gewobag",)            # need Playwright + Chromium

# Field names we deliberately never expose (honeypot / anti-spam).
HONEYPOT_NAMES = {"__hp", "honeypot"}

# A polite desktop UA, matching the live senders/scrapers.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
HTTP_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Default search-result URLs used to auto-pick a live listing per provider (they
# mirror the crawlers' URL patterns / config.example.yaml). Overridable per run
# with --listing.
DEFAULT_SEARCH_URLS = {
    "howoge": "https://www.howoge.de/immobiliensuche/wohnungssuche.html",
    "wbm": "https://www.wbm.de/wohnungen-berlin/angebote/",
    "gewobag": "https://www.gewobag.de/fuer-mietinteressentinnen/mietangebote/wohnung/",
}

# Howoge: obid lives in the listing URL as e.g. /detail/1771-14536-9997.html.
_HOWOGE_OBID_RE = re.compile(r"/detail/([0-9]+-[0-9]+-[0-9]+)")
_HOWOGE_FORM_URL = (
    "https://www.howoge.de/immobiliensuche/wohnungssuche/besichtigung-vereinbaren/"
    "bewerbungsprozess.html"
    "?tx_howrealestate_visitform%5Baction%5D=showVisitForm"
    "&tx_howrealestate_visitform%5Bcontroller%5D=Immoobject"
    "&tx_howrealestate_visitform%5Bobid%5D={obid}"
)

# Gewobag: the wohnungshelden iframe src sits in the static listing HTML.
# e.g. <iframe id="contact-iframe" src="https://app.wohnungshelden.de/public/listings//application?c=<token>">
_GEWOBAG_IFRAME_RE = re.compile(
    r"""<iframe[^>]*\bid=["']contact-iframe["'][^>]*\bsrc=["']([^"']+)["']""",
    re.IGNORECASE,
)
# Fallback: id and src can appear in either order.
_GEWOBAG_IFRAME_SRC_RE = re.compile(
    r"""src=["'](https?://app\.wohnungshelden\.de/[^"']*application\?c=[^"']+)["']""",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _attr_str(value: Any) -> str:
    """BeautifulSoup attrs can be str or list[str]; normalise to a single str."""
    if isinstance(value, list):
        return value[0] if value else ""
    return value if isinstance(value, str) else ""


def _truncate(text: str, limit: int = 80) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit]


def _make_field(
    name: str,
    ftype: str,
    label: str = "",
    required: bool = False,
    options: list[str] | None = None,
) -> dict[str, Any]:
    """Build a normalized field record, matching discovered_raw.json's shape."""
    return {
        "name": name,
        "type": ftype,
        "label": _truncate(label),
        "required": bool(required),
        "options": list(options or []),
    }


def _is_excluded_html_field(tag) -> bool:
    """True for hidden/submit/button/honeypot fields we never enumerate."""
    name = _attr_str(tag.get("name"))
    if not name:
        return True
    # Strip a trailing [] (array fields) and any tx_powermail wrapper to spot __hp.
    bare = name.rstrip("[]")
    bare_leaf = bare.rsplit("[", 1)[-1].rstrip("]")
    if bare_leaf in HONEYPOT_NAMES or name in HONEYPOT_NAMES:
        return True
    ftype = (_attr_str(tag.get("type")) or "").lower()
    if tag.name in ("input",) and ftype in ("hidden", "submit", "button", "image", "reset"):
        return True
    if tag.name == "button":
        return True
    return False


# --------------------------------------------------------------------------- #
# Listing discovery via the live crawlers
# --------------------------------------------------------------------------- #
def _build_crawler(provider: str):
    """Instantiate this repo's crawler for ``provider`` on a minimal config."""
    from berlin_flat_hunter.config import BerlinConfig
    from berlin_flat_hunter.crawlers.gewobag import Gewobag
    from berlin_flat_hunter.crawlers.howoge import Howoge
    from berlin_flat_hunter.crawlers.wbm import Wbm

    classes = {"howoge": Howoge, "wbm": Wbm, "gewobag": Gewobag}
    cls = classes.get(provider)
    if cls is None:
        raise RuntimeError(f"no crawler registered for provider {provider!r}")
    config = BerlinConfig(config={"urls": []})
    return cls(config)


def _first_live_listing(provider: str) -> str:
    """Return a live listing URL for ``provider`` using the project's crawler.

    Raises RuntimeError with a clear message if no listing can be obtained.
    """
    try:
        crawler = _build_crawler(provider)
    except Exception as exc:  # pragma: no cover - import/env problem
        raise RuntimeError(f"could not build crawler for {provider!r}: {exc}") from exc

    search_url = DEFAULT_SEARCH_URLS.get(provider)
    if not search_url:
        raise RuntimeError(f"no default search URL for provider {provider!r}")

    try:
        listings: Iterable[Any] = crawler.get_results(search_url)
    except Exception as exc:  # pragma: no cover - network dependent
        raise RuntimeError(f"{provider}: crawl failed: {exc}") from exc

    for listing in listings:
        url = _listing_url(listing)
        if url:
            return url
    raise RuntimeError(f"{provider}: crawler returned no live listings")


def _listing_url(listing: Any) -> str:
    """Pull a URL out of a crawler result (a dict expose or an object)."""
    if isinstance(listing, dict):
        return str(listing.get("url", "") or "")
    return str(getattr(listing, "url", "") or "")


def _resolve_listing(provider: str, override: str | None) -> str:
    """Use the --listing override if given, else auto-pick the first live one."""
    if override:
        return override
    return _first_live_listing(provider)


# --------------------------------------------------------------------------- #
# HTTP client (requests)
# --------------------------------------------------------------------------- #
class _RequestsClient:
    """Thin wrapper over ``requests.Session`` with a default per-request timeout.

    Exposes a ``.get(url)`` returning a ``requests.Response`` (``.status_code``,
    ``.content``, ``.text``) and a ``.close()``, so the discover_* functions can
    stay agnostic of the underlying HTTP library.
    """

    def __init__(self, timeout: float = 30.0) -> None:
        import requests

        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(HTTP_HEADERS)

    def get(self, url: str):
        return self._session.get(url, timeout=self._timeout, allow_redirects=True)

    def close(self) -> None:
        self._session.close()


def _http_client(timeout: float = 30.0) -> _RequestsClient:
    return _RequestsClient(timeout=timeout)


# --------------------------------------------------------------------------- #
# HTTP-based providers (howoge, wbm)
# --------------------------------------------------------------------------- #
def _fields_from_html_form(form) -> list[dict[str, Any]]:
    """Enumerate visible input/select/textarea fields of a BeautifulSoup form.

    Mirrors the field shape already stored in discovered_raw.json:
    radio/checkbox groups are emitted per-option (their visible label is the
    adjacent text), selects carry their <option> texts, plain inputs carry the
    associated <label>. Hidden/submit/button/honeypot fields are skipped.
    """
    from bs4 import Tag

    fields: list[dict[str, Any]] = []

    for tag in form.find_all(["input", "select", "textarea"]):
        if not isinstance(tag, Tag):
            continue
        if _is_excluded_html_field(tag):
            continue

        name = _attr_str(tag.get("name"))
        required = tag.has_attr("required") or (
            _attr_str(tag.get("aria-required")).lower() == "true"
        )
        label = _label_for_html(form, tag)

        if tag.name == "select":
            options = [
                opt.get_text(" ", strip=True)
                for opt in tag.find_all("option")
                if opt.get_text(strip=True) and _attr_str(opt.get("value")) != ""
            ]
            fields.append(_make_field(name, "select", label, required, options))
            continue

        if tag.name == "textarea":
            fields.append(_make_field(name, "textarea", label, required))
            continue

        # <input>
        ftype = (_attr_str(tag.get("type")) or "text").lower()
        fields.append(_make_field(name, ftype, label, required))

    return fields


def _label_for_html(form, tag) -> str:
    """Best-effort visible label for an HTML form control.

    Order: explicit ``<label for=id>`` -> wrapping ``<label>`` -> placeholder ->
    aria-label -> value (useful for radios/checkboxes). The existing raw dump
    uses the adjacent option text for radios, which is exactly the wrapping or
    for-target label here.
    """
    el_id = _attr_str(tag.get("id"))
    if el_id:
        lbl = form.find("label", attrs={"for": el_id})
        if lbl is not None:
            text = lbl.get_text(" ", strip=True)
            if text:
                return text

    parent_label = tag.find_parent("label")
    if parent_label is not None:
        text = parent_label.get_text(" ", strip=True)
        if text:
            return text

    placeholder = _attr_str(tag.get("placeholder"))
    if placeholder:
        return placeholder
    aria = _attr_str(tag.get("aria-label"))
    if aria:
        return aria
    # For radios/checkboxes the value is often the human-readable option.
    return _attr_str(tag.get("value"))


def discover_howoge(client, listing_url: str) -> dict[str, Any]:
    """Discover the Howoge visit-request form fields (plain HTML)."""
    from bs4 import BeautifulSoup, Tag

    match = _HOWOGE_OBID_RE.search(listing_url)
    if not match:
        raise RuntimeError(f"could not extract obid from listing URL: {listing_url}")
    obid = match.group(1)
    form_url = _HOWOGE_FORM_URL.format(obid=obid)

    resp = client.get(form_url)
    status = resp.status_code
    if status != 200:
        raise RuntimeError(f"form GET HTTP {status} ({form_url})")

    soup = BeautifulSoup(resp.content, "lxml")
    form = soup.find("form", id="show-visit-form")
    if not isinstance(form, Tag):
        raise RuntimeError("howoge visit form (#show-visit-form) not found — layout changed")

    fields = _fields_from_html_form(form)
    return {
        "listing": listing_url,
        "status": status,
        "fields": fields,
    }


def discover_wbm(client, listing_url: str) -> dict[str, Any]:
    """Discover the WBM powermail contact form fields (plain HTML)."""
    from bs4 import BeautifulSoup, Tag

    resp = client.get(listing_url)
    status = resp.status_code
    if status != 200:
        raise RuntimeError(f"listing GET HTTP {status} ({listing_url})")

    soup = BeautifulSoup(resp.content, "lxml")
    form = soup.find("form", class_="powermail_form")
    if not isinstance(form, Tag):
        # Some WBM templates render the form without the class on <form>; fall back.
        form = soup.select_one("form.powermail_form, form[id*='powermail']")
    if not isinstance(form, Tag):
        raise RuntimeError("WBM powermail form not found — layout changed")

    fields = _fields_from_html_form(form)
    return {
        "listing": listing_url,
        "status": status,
        "fields": fields,
    }


# --------------------------------------------------------------------------- #
# Gewobag — iframe src extraction (HTTP) + Playwright SPA enumeration
# --------------------------------------------------------------------------- #
def extract_gewobag_iframe_src(html: str, base_url: str = "") -> str:
    """Regex the wohnungshelden contact-iframe src out of the listing HTML.

    Returns an absolute URL, or "" if not found. Works on the *static* HTML, so
    no browser is needed for this step.
    """
    match = _GEWOBAG_IFRAME_RE.search(html)
    if match is None:
        match = _GEWOBAG_IFRAME_SRC_RE.search(html)
    if match is None:
        return ""
    src = match.group(1).strip()
    if src and not src.startswith("http") and base_url:
        src = urljoin(base_url, src)
    return src


def _gewobag_fetch_iframe_src(client, listing_url: str) -> dict[str, Any]:
    """Fetch the gewobag listing and pull the iframe src + a couple of stats."""
    resp = client.get(listing_url)
    status = resp.status_code
    if status != 200:
        raise RuntimeError(f"listing GET HTTP {status} ({listing_url})")
    html = resp.text
    iframe_src = extract_gewobag_iframe_src(html, base_url=listing_url)
    return {
        "listing": listing_url,
        "status": status,
        "iframe_in_static_html": bool(iframe_src),
        "iframe_src": iframe_src,
        "has_listing_id": bool(_iframe_listing_id(iframe_src)),
        "wohnungshelden_mentions": html.lower().count("wohnungshelden"),
    }


def _iframe_listing_id(iframe_src: str) -> str:
    """Extract the wohnungshelden listing id from ``/listings/<id>/application``.

    Many Gewobag listings ship an EMPTY id (``listings//application``) — those
    SPAs render "Fehler beim Laden" and have no form. Returns "" for those.
    """
    m = re.search(r"/listings/([^/]+)/application", iframe_src or "")
    return (m.group(1).strip() if m else "")


def _find_gewobag_form_listing(client, max_scan: int = 15) -> dict[str, Any]:
    """Scan live Gewobag listings for one whose iframe carries a real listing id
    (i.e. actually has a working application form). Returns the iframe-src info
    dict for the first usable listing, or the last-seen one if none qualify."""
    try:
        crawler = _build_crawler("gewobag")
        listings = list(crawler.get_results(DEFAULT_SEARCH_URLS["gewobag"]))
    except Exception as exc:  # pragma: no cover - env dependent
        raise RuntimeError(f"could not list gewobag listings: {exc}") from exc

    last: dict[str, Any] | None = None
    scanned = 0
    for listing in listings:
        url = _listing_url(listing)
        if not url:
            continue
        scanned += 1
        if scanned > max_scan:
            break
        try:
            info = _gewobag_fetch_iframe_src(client, url)
        except Exception:
            continue
        last = info
        if info.get("has_listing_id"):
            info["scanned"] = scanned
            return info
    if last is None:
        raise RuntimeError("no gewobag listings could be fetched")
    last["scanned"] = scanned
    last["note"] = "no listing with a non-empty form id found in the scan window"
    return last


def _playwright_available() -> tuple[bool, str]:
    """Is the Playwright python package importable? (browser checked at launch)."""
    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright  # noqa: F401
    except Exception as exc:  # pragma: no cover - env dependent
        return False, f"Playwright not installed ({exc})"
    return True, ""


def _enumerate_gewobag_spa(iframe_src: str, timeout_ms: int = 30_000) -> list[dict[str, Any]]:
    """Drive headless Chromium to the SPA URL and enumerate formcontrolname fields.

    Returns the normalized field list. Raises on browser launch / navigation
    failure so the caller can record it as a per-provider error (and still keep
    the iframe-src info it already gathered).
    """
    from playwright.sync_api import sync_playwright

    fields: list[dict[str, Any]] = []
    with sync_playwright() as pw:
        # --no-sandbox / --disable-dev-shm-usage keep Chromium happy in
        # containers and on low-memory hosts.
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            page = browser.new_page(user_agent=USER_AGENT, viewport={"width": 1280, "height": 2400})
            page.goto(iframe_src, wait_until="domcontentloaded", timeout=timeout_ms)
            # The Angular form renders its inputs asynchronously. Wait for at least
            # one formcontrolname control to be ATTACHED (not necessarily visible —
            # some sit in collapsed sections) then let the form settle.
            page.wait_for_selector(
                "input[formcontrolname], textarea[formcontrolname]",
                state="attached",
                timeout=timeout_ms,
            )
            page.wait_for_timeout(2500)
            fields = _extract_spa_fields(page)
        finally:
            browser.close()
    return fields


def _extract_spa_fields(page) -> list[dict[str, Any]]:
    """Read every formcontrolname input/select/textarea + label/type/options.

    Runs a single in-page JS function so we get the rendered DOM (labels,
    mat-label, placeholders, mat-option texts, required state) in one round-trip.
    """
    js = r"""
    () => {
      const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();

      const labelFor = (el) => {
        // 1) <label for="id">
        const id = el.getAttribute('id');
        if (id) {
          const l = document.querySelector('label[for="' + (window.CSS && CSS.escape ? CSS.escape(id) : id) + '"]');
          if (l && norm(l.innerText)) return norm(l.innerText);
        }
        // 2) wrapping <label>
        let p = el.closest('label');
        if (p && norm(p.innerText)) return norm(p.innerText);
        // 3) Angular Material field: nearest mat-form-field's mat-label
        const field = el.closest('mat-form-field, .mat-form-field, .mat-mdc-form-field, nz-form-item, .ant-form-item');
        if (field) {
          const ml = field.querySelector('mat-label, label, .mat-form-field-label, .ant-form-item-label');
          if (ml && norm(ml.innerText)) return norm(ml.innerText);
        }
        // 4) aria-label / placeholder / formcontrolname
        return norm(el.getAttribute('aria-label'))
            || norm(el.getAttribute('placeholder'))
            || norm(el.getAttribute('formcontrolname'));
      };

      const isRequired = (el) => {
        if (el.hasAttribute('required')) return true;
        const ar = el.getAttribute('aria-required');
        if (ar && ar.toLowerCase() === 'true') return true;
        return false;
      };

      const out = [];
      const sel = 'input[formcontrolname], select[formcontrolname], textarea[formcontrolname]';
      document.querySelectorAll(sel).forEach((el) => {
        const name = el.getAttribute('formcontrolname') || '';
        if (!name) return;
        const tag = el.tagName.toLowerCase();
        let type = tag;
        if (tag === 'input') type = (el.getAttribute('type') || 'text').toLowerCase();

        // Options: native <select><option>, plus Angular Material <mat-option>
        // / Ant <nz-option> rendered in an overlay referenced by the control.
        let options = [];
        if (tag === 'select') {
          options = Array.from(el.querySelectorAll('option'))
            .filter((o) => o.value !== '')
            .map((o) => norm(o.innerText));
        } else {
          const field = el.closest('mat-form-field, .mat-form-field, nz-select, .ant-select');
          if (field) {
            const opts = field.querySelectorAll('mat-option, nz-option, .ant-select-item-option-content');
            options = Array.from(opts).map((o) => norm(o.innerText)).filter(Boolean);
          }
        }

        out.push({
          name: name,
          type: type,
          label: labelFor(el),
          required: isRequired(el),
          options: options,
        });
      });
      return out;
    }
    """
    raw = page.evaluate(js)
    fields: list[dict[str, Any]] = []
    for item in raw or []:
        name = (item.get("name") or "").strip()
        if not name or name in HONEYPOT_NAMES:
            continue
        fields.append(
            _make_field(
                name=name,
                ftype=item.get("type") or "text",
                label=item.get("label") or "",
                required=bool(item.get("required")),
                options=list(item.get("options") or []),
            )
        )
    return fields


def discover_gewobag(
    client, listing_url: str, *, use_browser: bool
) -> dict[str, Any]:
    """Discover the Gewobag (wohnungshelden SPA) form.

    Always returns the iframe-src info (HTTP-only). If ``use_browser`` is set and
    Playwright + Chromium are available, also enumerates the SPA fields. A
    missing browser is reported via a ``browser_note`` key, never a crash.
    """
    result = _gewobag_fetch_iframe_src(client, listing_url)

    # Many Gewobag listings have an EMPTY form id (listings//application) and no
    # working form. If the picked one is empty (and the user didn't force a
    # specific --listing), scan for one that actually has a form.
    if not result.get("has_listing_id") and not _LISTING_OVERRIDDEN:
        try:
            scanned = _find_gewobag_form_listing(client)
            if scanned.get("has_listing_id"):
                result = scanned
        except Exception as exc:
            result.setdefault("note", f"form-listing scan failed: {exc}")

    iframe_src = result.get("iframe_src") or ""

    if not use_browser:
        result["browser_note"] = "browser disabled (--no-browser); fields not enumerated"
        return result

    if not iframe_src:
        result["browser_note"] = "no iframe src found — cannot launch SPA"
        return result
    if not result.get("has_listing_id"):
        result["browser_note"] = (
            "no Gewobag listing with a populated form id was found right now "
            "(many list 'listings//application'); try again later or pass --listing"
        )
        return result

    ok, why = _playwright_available()
    if not ok:
        result["browser_note"] = (
            f"{why} — install with 'pip install playwright && playwright install chromium'"
        )
        return result

    # Playwright is importable; the browser may still fail to launch (e.g. in a
    # sandbox). Surface that as a note and re-raise so the caller records it as a
    # per-provider error, while the iframe-src info is already captured.
    try:
        fields = _enumerate_gewobag_spa(iframe_src)
    except Exception as exc:
        result["browser_note"] = (
            f"Chromium could not render the SPA: {exc}. "
            "Run on a laptop with a working Chromium (playwright install chromium)."
        )
        # Re-raise wrapped so the per-provider error report mentions it too, but
        # keep the partial result available via the exception's payload.
        raise GewobagBrowserError(str(exc), partial=result) from exc

    result["fields"] = fields
    return result


class GewobagBrowserError(RuntimeError):
    """Raised when the SPA could not be rendered; carries the partial result."""

    def __init__(self, message: str, partial: dict[str, Any]) -> None:
        super().__init__(message)
        self.partial = partial


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def discover(
    providers: list[str],
    *,
    listing_override: str | None,
    use_browser: bool,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Run discovery for each provider. Returns (results, errors-by-provider)."""
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}

    # One shared HTTP client for all HTTP work.
    client = None
    try:
        client = _http_client()
    except Exception as exc:
        # requests missing is fatal for every provider; record and bail cleanly.
        for prov in providers:
            errors[prov] = f"requests unavailable: {exc}"
        return results, errors

    try:
        for provider in providers:
            try:
                listing = _resolve_listing(provider, listing_override)
            except Exception as exc:
                errors[provider] = f"listing discovery failed: {exc}"
                continue

            try:
                if provider == "howoge":
                    results[provider] = discover_howoge(client, listing)
                elif provider == "wbm":
                    results[provider] = discover_wbm(client, listing)
                elif provider == "gewobag":
                    results[provider] = discover_gewobag(
                        client, listing, use_browser=use_browser
                    )
                else:
                    errors[provider] = f"unknown provider {provider!r}"
            except GewobagBrowserError as exc:
                # Keep the partial (iframe-src) result; record the browser error.
                results[provider] = exc.partial
                errors[provider] = str(exc)
            except Exception as exc:
                errors[provider] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            if client is not None:
                client.close()
        except Exception:
            pass

    return results, errors


def merge_into_existing(out_path: Path, fresh: dict[str, Any]) -> dict[str, Any]:
    """Load any existing JSON at out_path and overlay the freshly discovered
    providers (preserving providers we didn't re-run this time)."""
    merged: dict[str, Any] = {}
    if out_path.exists():
        try:
            merged = json.loads(out_path.read_text(encoding="utf-8"))
            if not isinstance(merged, dict):
                merged = {}
        except Exception:
            merged = {}
    merged.update(fresh)
    return merged


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _field_count(entry: Any) -> int:
    if isinstance(entry, dict) and isinstance(entry.get("fields"), list):
        return len(entry["fields"])
    return 0


def _sample_names(entry: Any, n: int = 4) -> str:
    if isinstance(entry, dict) and isinstance(entry.get("fields"), list):
        names = [f.get("name", "") for f in entry["fields"][:n]]
        suffix = ", …" if len(entry["fields"]) > n else ""
        return ", ".join(names) + suffix
    return "(no fields)"


def print_summary(results: dict[str, Any], errors: dict[str, str], providers: list[str]) -> None:
    print("\n=== Discovery summary ===")
    header = f"{'provider':<10} | {'fields':>6} | sample field names"
    print(header)
    print("-" * max(len(header), 60))
    for provider in providers:
        entry = results.get(provider)
        if entry is None:
            print(f"{provider:<10} | {'-':>6} | ERROR: {errors.get(provider, 'no result')}")
            continue
        count = _field_count(entry)
        if provider == "gewobag" and count == 0:
            note = entry.get("browser_note", "")
            iframe = "iframe-src OK" if entry.get("iframe_src") else "iframe-src MISSING"
            print(f"{provider:<10} | {count:>6} | {iframe}; {note}")
        else:
            print(f"{provider:<10} | {count:>6} | {_sample_names(entry)}")

    if errors:
        print("\n--- Errors ---")
        for provider, msg in errors.items():
            print(f"  {provider}: {msg}")


def print_gewobag_mapping(results: dict[str, Any]) -> None:
    """Print a paste-ready mapping block for the maintainer.

    For each discovered gewobag formcontrolname, emit a comment line noting which
    canonical catalog key (if any) currently maps to it in catalog.py.
    """
    entry = results.get("gewobag")
    if not isinstance(entry, dict):
        return
    fields = entry.get("fields")
    if not isinstance(fields, list) or not fields:
        print(
            "\n--- Gewobag mapping ---\n"
            "  (no SPA fields enumerated — run with a working Chromium to refresh "
            "the gewobag mapping; iframe src was "
            + ("captured)" if entry.get("iframe_src") else "NOT captured)")
        )
        return

    # Reverse-lookup against the live catalog if importable.
    reverse = {}
    try:
        from berlin_flat_hunter.forms import catalog as form_catalog  # noqa: WPS433

        for q in form_catalog.CATALOG:
            fname = q.providers.get("gewobag")
            if fname:
                reverse[fname] = q.key
    except Exception:
        reverse = {}

    print("\n--- Suggested catalog.py mapping for Gewobag ---")
    print("# Paste/verify against CATALOG entries' providers={\"gewobag\": ...}:")
    for f in fields:
        name = f.get("name", "")
        canonical = reverse.get(name)
        if canonical:
            print(f'#   gewobag: {name} -> maps to canonical key "{canonical}" (label: {f.get("label", "")})')
        else:
            print(f'#   gewobag: {name} -> (maps to canonical key?)  label: {f.get("label", "")}')


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="discover_forms.py",
        description=(
            "Scrape the live Berlin Genossenschaft application forms and "
            "enumerate every field, to keep catalog.py in sync."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--providers",
        default=",".join(ALL_PROVIDERS),
        help="comma-separated providers to discover (howoge,wbm,gewobag).",
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help="path to write the merged discovery JSON.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="skip Playwright/gewobag SPA enumeration (HTTP providers still run).",
    )
    parser.add_argument(
        "--listing",
        default=None,
        help="discover a specific listing URL instead of auto-picking the first live one.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    global _LISTING_OVERRIDDEN
    args = parse_args(argv)
    _LISTING_OVERRIDDEN = bool(args.listing)

    requested = [p.strip().lower() for p in args.providers.split(",") if p.strip()]
    unknown = [p for p in requested if p not in ALL_PROVIDERS]
    if unknown:
        print(f"Unknown provider(s): {', '.join(unknown)} "
              f"(valid: {', '.join(ALL_PROVIDERS)})", file=sys.stderr)
        return 2
    if not requested:
        requested = list(ALL_PROVIDERS)

    use_browser = not args.no_browser
    # If --no-browser, drop browser-only providers from the run but keep HTTP ones.
    providers = [
        p for p in requested
        if use_browser or p in HTTP_PROVIDERS
    ]
    skipped_browser = [p for p in requested if p not in providers]
    if skipped_browser:
        print(f"--no-browser: skipping browser-only provider(s): "
              f"{', '.join(skipped_browser)}")

    if args.listing and len(providers) != 1:
        print("Note: --listing applies to every selected provider; usually pair it "
              "with a single --providers value.", file=sys.stderr)

    print(f"Discovering: {', '.join(providers) or '(none)'}  "
          f"(browser={'on' if use_browser else 'off'})")

    results, errors = discover(
        providers,
        listing_override=args.listing,
        use_browser=use_browser,
    )

    # Merge with whatever is already on disk and write it back.
    out_path = Path(args.out)
    merged = merge_into_existing(out_path, results)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"\nWrote {out_path} ({len(merged)} provider(s) total).")
    except Exception as exc:
        print(f"Failed to write {out_path}: {exc}", file=sys.stderr)

    print_summary(results, errors, providers)
    if "gewobag" in providers:
        print_gewobag_mapping(results)

    # Exit non-zero only if EVERY requested provider errored (so CI can detect a
    # total failure, but a single flaky provider doesn't fail the whole run).
    if providers and len(errors) >= len(providers) and not results:
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
    except Exception:  # pragma: no cover - last-ditch guard
        traceback.print_exc()
        raise SystemExit(1)
