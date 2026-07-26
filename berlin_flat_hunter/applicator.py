"""AutoApplicator — Selenium-based form submission for Gewobag, WBM, and Kleinanzeigen.

Caveat: form structures change frequently. Selectors verified against live sites
on 2026-04-26. See README.md for current status. Use ``dry_run: true`` in config
to fill forms without submitting (recommended on first use).
"""
import os
import re
import time
from typing import Any, Callable, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

from flathunter.abstract_processor import Processor
from flathunter.logging import logger

# After this many consecutive URL-matched apply() failures for the same site,
# we assume selectors are stale and push a notifier alert. Cool-down avoids
# spamming when every listing in a hunt cycle hits the same broken site.
_STALE_THRESHOLD = 3
_STALE_ALERT_COOLDOWN = 3600  # seconds


class ManualApplyRequired(Exception):
    """Raised by an applicator when an external block prevents automated
    submission for a listing — e.g. Gewobag reCAPTCHA, Kleinanzeigen login
    wall with no credentials configured. The ``AutoApplicator`` catches this
    and dispatches a per-listing notification through the alert chain so the
    user knows to apply manually, instead of letting the listing silently
    fall through.

    This is distinct from a "stale selectors" failure: stale selectors mean
    the applicator code is broken; ``ManualApplyRequired`` means the code
    works but the *site* deliberately requires a human.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _chrome_driver(headless: bool = True, profile_dir: Optional[str] = None):
    """Construct a Chrome WebDriver.

    When ``profile_dir`` is set, the driver loads (or creates) a persistent
    Chrome profile at that path and runs *headed* via undetected-chromedriver.
    Persisting the profile keeps cookies/local-storage/login state across
    runs, which dramatically reduces reCAPTCHA prompts for sites that score
    trust on session warmth. The Docker ``vnc`` target supplies the required
    virtual display (Xvfb on ``:99``) and a noVNC web client on port 6080
    for occasional manual login / CAPTCHA solving.

    When ``profile_dir`` is unset the driver falls back to plain headless
    Chrome — appropriate for dry-run testing or sites without bot protection.
    """
    if profile_dir:
        try:
            import undetected_chromedriver as uc
        except ImportError as exc:
            raise ImportError(
                "undetected-chromedriver is required when chrome_profile_dir "
                "is set; install via `uv sync`"
            ) from exc
        os.makedirs(profile_dir, exist_ok=True)
        options = uc.ChromeOptions()
        options.add_argument(f"--user-data-dir={profile_dir}")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        # Do NOT set --headless: undetected-chromedriver disables it anyway,
        # and the whole point of this path is real-headed Chrome rendered
        # into Xvfb so reCAPTCHA cannot trivially fingerprint automation.
        return uc.Chrome(options=options, use_subprocess=True)

    from selenium import webdriver
    options = webdriver.ChromeOptions()
    if headless:
        # `--headless=new` is the supported flag on Chrome 109+; legacy
        # `--headless` is deprecated and emits a console warning.
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    return webdriver.Chrome(options=options)


def _fill_field(driver, selector: str, value: str) -> bool:
    """Try each CSS selector in a comma-separated list; fill first match. Returns True if filled.

    Catches ``NoSuchElementException`` and ``ElementNotInteractableException``
    only — these are the expected "selector did not match / element hidden"
    paths and we want to fall through to the next selector. Driver wedges
    (``WebDriverException``, ``TimeoutException``) propagate so the caller
    can recycle the session instead of silently logging "no fields filled".
    """
    if not value:
        return False
    from selenium.common.exceptions import (
        ElementNotInteractableException,
        NoSuchElementException,
    )
    for raw in selector.split(","):
        sel = raw.strip()
        if not sel:
            continue
        try:
            el = driver.find_element("css selector", sel)
            el.clear()
            el.send_keys(value)
            return True
        except (NoSuchElementException, ElementNotInteractableException):
            continue
    return False


_COMMON_SELECTORS = {
    "name": "input[name*='name'], input[id*='name']",
    "email": "input[type='email'], input[name*='email'], input[id*='email']",
    "phone": "input[type='tel'], input[name*='phone'], input[id*='phone']",
    "message": "textarea",
    "submit": "button[type='submit'], input[type='submit']",
}


class _AttrView:
    """Expose a dict's keys as attributes (missing → "") so the form catalog's
    getattr-based applicant lookups don't raise on our plain applicant dict."""

    def __init__(self, d: dict):
        self._d = d or {}

    def __getattr__(self, name):
        return self._d.get(name, "")


def _wbm_answer_fields(applicant: dict, form_answers: dict) -> dict:
    """``{powermail_field_name: value}`` for WBM's questionnaire-backed fields
    (Anrede / WBS / income) drawn from ``form_answers`` via the shared form
    catalog. Applicant-backed fields resolve empty (different key names) and are
    dropped; the consent field is excluded (its checkbox is ticked separately).
    Best-effort — returns ``{}`` if the catalog is unavailable."""
    if not form_answers:
        return {}
    try:
        from berlin_flat_hunter.forms import catalog
        vals = catalog.values_for_provider("wbm", _AttrView(applicant), form_answers)
    except Exception as exc:  # noqa: BLE001
        logger.debug("WBM form-answer mapping unavailable: %s", exc)
        return {}
    return {k: v for k, v in vals.items() if v and k != "datenschutzhinweis"}


class GewobagApplicator:
    """Submit Anfrage form on a Gewobag listing detail page.

    Layout (verified live 2026-04-28 by rendering the iframe in headless
    Chromium and dumping the Angular DOM): the page exposes a button
    ``button.rental-contact[data-tab='rental-contact']`` ("Anfrage senden")
    which reveals a tab containing ``iframe#contact-iframe`` pointing at
    ``app.wohnungshelden.de``. The application form is an Angular Reactive-
    Forms / NG-ZORRO SPA inside that iframe with these named controls:

    - ``firstName``, ``lastName``, ``email``, ``phoneNumber``
    - ``street``, ``houseNumber``, ``zipCode``, ``city``
    - ``applicantMessage`` (textarea)
    - a Gewobag-specific privacy checkbox with ``id`` matching
      ``*datenschutz*`` (the rest of the id is dynamically generated per page)
    - submit button identified by ``[data-cy='btn-submit']``

    The form ALSO includes (which we cannot fully automate):
    - A salutation combobox (``role='combobox'``, NG-ZORRO ``nz-select``)
    - "Für wen wird die Wohnungsanfrage gestellt" combobox + nested first/last
      name/phone if filled on someone else's behalf
    - "Gesamtzahl der einziehenden Personen" required number input
    - **Google reCAPTCHA** — invisible challenge that must be solved before
      submit will be accepted by the wohnungshelden backend.

    Practically this means **dry-run mode reliably reports field coverage**,
    but live submission fails at the reCAPTCHA step regardless of how many
    fields we fill. We detect reCAPTCHA presence and abort live submit with a
    clear error (logged + surfaced via the AutoApplicator alert chain).
    """

    URL_MATCH = "gewobag.de"
    SITE_NAME = "Gewobag"

    _IFRAME = "iframe#contact-iframe"

    # Selectors verified against rendered NG-ZORRO DOM (2026-04-28). Each
    # cascade prefers the canonical formcontrolname binding then falls back
    # to id/name patterns for forward-compatibility with minor renames.
    _SEL_FIRSTNAME = (
        "input[formcontrolname='firstName'], input#firstName, "
        "input[data-cy='firstName']"
    )
    _SEL_LASTNAME = (
        "input[formcontrolname='lastName'], input#lastName, "
        "input[data-cy='lastName']"
    )
    _SEL_EMAIL = (
        "input[formcontrolname='email'], input#email, "
        "input[data-cy='email']"
    )
    _SEL_PHONE = (
        "input[formcontrolname='phoneNumber'], input#phone-number, "
        "input[data-cy='phone-number']"
    )
    _SEL_MESSAGE = (
        "textarea[formcontrolname='applicantMessage'], textarea#applicant-message, "
        "textarea[data-cy='applicant-message']"
    )
    _SEL_STREET = "input[formcontrolname='street'], input#street, input[data-cy='street']"
    _SEL_HOUSENUMBER = (
        "input[formcontrolname='houseNumber'], input#house-number, "
        "input[data-cy='house-number']"
    )
    _SEL_ZIP = "input[formcontrolname='zipCode'], input#zip-code, input[data-cy='zip-code']"
    _SEL_CITY = "input[formcontrolname='city'], input#city, input[data-cy='city']"
    _SEL_PRIVACY = "input[type='checkbox'][id*='datenschutz']"
    _SEL_SUBMIT = "button[data-cy='btn-submit'], button.ant-btn-primary[type='submit']"
    # reCAPTCHA presence indicator — wohnungshelden injects this textarea even
    # when the badge is invisible, so it's a reliable detection signal.
    _SEL_RECAPTCHA = "textarea.g-recaptcha-response, iframe[src*='recaptcha']"

    def __init__(self, applicant: dict, dry_run: bool = False, profile_dir: Optional[str] = None):
        self.applicant = applicant
        self.dry_run = dry_run
        self.profile_dir = profile_dir or None

    def close(self):
        """No persistent state — driver is per-apply. Defined for symmetry with the pool API."""
        return None

    def apply(self, expose: dict) -> bool:
        url = expose.get("url", "")
        if self.URL_MATCH not in url:
            return False
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait

            driver = _chrome_driver(profile_dir=self.profile_dir)
            try:
                # 1. Load the listing page only long enough to read the iframe
                #    src — the wohnungshelden form lives inside a hidden tab,
                #    and forcing the tab open via JS click is fragile in
                #    headless Chrome (inputs end up displayed=False because the
                #    parent ``.content-tab`` div has ``display:none``). It is
                #    simpler and more reliable to navigate the driver directly
                #    to the iframe's URL — wohnungshelden serves the SPA at
                #    that URL standalone, with all inputs as top-level visible
                #    elements. The src is per-listing (contains a token), so
                #    we cannot hard-code it.
                driver.get(url)
                try:
                    iframe_el = WebDriverWait(driver, 15).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, self._IFRAME))
                    )
                except Exception:
                    logger.warning("Gewobag: contact iframe not found on %s — "
                                   "page layout changed", url)
                    return False
                iframe_src = iframe_el.get_attribute("src") or ""
                if not iframe_src.startswith("https://"):
                    logger.warning("Gewobag: iframe src missing or invalid on %s "
                                   "(got %r)", url, iframe_src)
                    return False

                # 2. Navigate to the standalone SPA URL — now inputs are
                #    top-level and visible, no tab juggling required.
                driver.get(iframe_src)

                # 3. Wait for the Angular SPA to mount its form. We wait for
                #    visibility (not just presence) since we'll be calling
                #    el.clear() / send_keys, which only work on interactable
                #    elements.
                try:
                    WebDriverWait(driver, 30).until(EC.visibility_of_element_located((
                        By.CSS_SELECTOR, "input[formcontrolname], textarea[formcontrolname]",
                    )))
                except Exception:
                    logger.warning("Gewobag: wohnungshelden SPA did not render any "
                                   "form inputs for %s — site may be down", url)
                    return False

                # 4. Fill all known fields. wohnungshelden splits name into first +
                #    last; use the WBM-style splitter (last-name-dominant in DACH).
                last, first = WbmApplicator._split_name(self.applicant.get("name", ""))
                filled = sum([
                    _fill_field(driver, self._SEL_FIRSTNAME, first),
                    _fill_field(driver, self._SEL_LASTNAME, last or self.applicant.get("name", "")),
                    _fill_field(driver, self._SEL_EMAIL, self.applicant.get("email", "")),
                    _fill_field(driver, self._SEL_PHONE, self.applicant.get("phone", "")),
                    _fill_field(driver, self._SEL_MESSAGE, self.applicant.get("message", "")),
                    _fill_field(driver, self._SEL_STREET, self.applicant.get("street", "")),
                    _fill_field(driver, self._SEL_HOUSENUMBER, self.applicant.get("house_number", "")),
                    _fill_field(driver, self._SEL_ZIP, self.applicant.get("postal_code", "")),
                    _fill_field(driver, self._SEL_CITY, self.applicant.get("city", "")),
                ])
                if filled == 0:
                    logger.warning("Gewobag: no form fields matched inside iframe on %s — "
                                   "wohnungshelden form schema likely changed", url)
                    return False

                # 5. Privacy checkbox — Gewobag-specific dynamic id (formly_NN_checkbox_…).
                #    Required for the SPA's submit validation. NG-ZORRO often hides the
                #    real input behind a styled wrapper, so we click via JS as fallback.
                try:
                    box = driver.find_element(By.CSS_SELECTOR, self._SEL_PRIVACY)
                    if not box.is_selected():
                        try:
                            box.click()
                        except Exception:
                            driver.execute_script("arguments[0].click();", box)
                except Exception:
                    logger.debug("Gewobag: no privacy checkbox visible — assuming not required")

                if self.dry_run:
                    logger.info("Gewobag dry-run: %d fields filled but submit skipped for %s",
                                filled, url)
                    return True

                # 6. reCAPTCHA gate — wohnungshelden runs Google reCAPTCHA on
                #    submit. Selenium cannot solve it. Raise ManualApplyRequired
                #    so the AutoApplicator dispatches a per-listing notification
                #    (telegram/etc.) prompting the user to submit manually,
                #    instead of treating this as a regular failure.
                if driver.find_elements(By.CSS_SELECTOR, self._SEL_RECAPTCHA):
                    raise ManualApplyRequired("reCAPTCHA challenge")
                try:
                    driver.find_element(By.CSS_SELECTOR, self._SEL_SUBMIT).click()
                except Exception as exc:
                    logger.warning("Gewobag: submit button not clickable on %s: %s", url, exc)
                    return False
                time.sleep(2)
                logger.info("Gewobag application submitted for %s", url)
                return True
            finally:
                driver.quit()
        except ManualApplyRequired:
            raise  # AutoApplicator turns this into a manual-apply notification
        except Exception as exc:
            logger.warning("Gewobag application failed for %s: %s", url, exc)
            return False


class WbmApplicator:
    """Submit WBM (powermail) contact form on a WBM listing detail page.

    WBM listings are mostly WBS-restricted; the form requires WBS info,
    salutation, full name+address, and a privacy checkbox. We fill the basic
    contact fields plus the privacy checkbox; WBS fields are left at their
    defaults. Use ``dry_run: true`` to verify before going live.
    """

    URL_MATCH = "wbm.de"
    SITE_NAME = "WBM"

    # Real WBM powermail field IDs (verified live 2026-04-26)
    _FIELD_LASTNAME = "input#powermail_field_name"
    _FIELD_FIRSTNAME = "input#powermail_field_vorname"
    _FIELD_EMAIL = "input#powermail_field_e_mail"
    _FIELD_PHONE = "input#powermail_field_telefon"
    _FIELD_STREET = "input#powermail_field_strasse"
    _FIELD_PLZ = "input#powermail_field_plz"
    _FIELD_CITY = "input#powermail_field_ort"
    _PRIVACY_CHECKBOX = "input#powermail_field_datenschutzhinweis_1"
    _SUBMIT = "form.powermail_form button[type='submit']"

    def __init__(self, applicant: dict, dry_run: bool = False, profile_dir: Optional[str] = None):
        self.applicant = applicant
        self.dry_run = dry_run
        self.profile_dir = profile_dir or None

    def close(self):
        """No persistent state — driver is per-apply."""
        return None

    def apply(self, expose: dict) -> bool:
        url = expose.get("url", "")
        if self.URL_MATCH not in url:
            return False
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait

            driver = _chrome_driver(profile_dir=self.profile_dir)
            try:
                driver.get(url)
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "form.powermail_form"))
                )
                # WBM overlays a Klaro cookie-consent modal on top of the
                # form. Without dismissing it the privacy checkbox and submit
                # button aren't clickable — Selenium throws "element click
                # intercepted" and the whole apply silently fails.
                self._dismiss_cookie_banner(driver)
                last, first = self._split_name(self.applicant.get("name", ""))
                filled = sum([
                    _fill_field(driver, self._FIELD_LASTNAME, last),
                    _fill_field(driver, self._FIELD_FIRSTNAME, first),
                    _fill_field(driver, self._FIELD_EMAIL, self.applicant.get("email", "")),
                    _fill_field(driver, self._FIELD_PHONE, self.applicant.get("phone", "")),
                    _fill_field(driver, self._FIELD_STREET, self.applicant.get("street", "")),
                    _fill_field(driver, self._FIELD_PLZ, self.applicant.get("postal_code", "")),
                    _fill_field(driver, self._FIELD_CITY, self.applicant.get("city", "Berlin")),
                ])
                # Tick privacy checkbox (required for submit)
                try:
                    box = driver.find_element(By.CSS_SELECTOR, self._PRIVACY_CHECKBOX)
                    if not box.is_selected():
                        box.click()
                except Exception:
                    logger.warning("WBM: privacy checkbox not found on %s", url)

                if filled == 0:
                    logger.warning("WBM: no form fields matched on %s — selectors may be stale", url)
                    return False

                # Extra questionnaire answers (Anrede/WBS/income) → powermail
                # fields. Best-effort; wrapped so it never breaks the core apply.
                # NB: field ids assumed to be powermail_field_<catalog-name>;
                # verify against the live form (scripts/discover_forms.py) before
                # relying on WBS/income delivery.
                for field_name, value in _wbm_answer_fields(self.applicant,
                                                            getattr(self, "form_answers", {})).items():
                    _fill_field(driver,
                                f"#powermail_field_{field_name}, "
                                f"input[name*='{field_name}'], select[name*='{field_name}']",
                                value)

                if self.dry_run:
                    logger.info("WBM dry-run: %d fields filled but submit skipped for %s",
                                filled, url)
                    return True
                driver.find_element(By.CSS_SELECTOR, self._SUBMIT).click()
                time.sleep(2)
                logger.info("WBM application submitted for %s", url)
                return True
            finally:
                driver.quit()
        except Exception as exc:
            logger.warning("WBM application failed for %s: %s", url, exc)
            return False

    @staticmethod
    def _split_name(full_name: str) -> tuple[str, str]:
        """Split 'Max Mustermann' into ('Mustermann', 'Max'); single-word names stay together."""
        parts = full_name.strip().split(None, 1)
        if len(parts) == 2:
            return parts[1], parts[0]
        return full_name, ""

    @staticmethod
    def _dismiss_cookie_banner(driver) -> None:
        """Best-effort dismissal of WBM's Klaro cookie-consent modal.

        Tries the modern Klaro selectors first, then a couple of legacy
        variants. Never raises — the applicator continues even if no banner
        was present or all selectors miss (element-click-intercepted is the
        real signal we care about, and that gets caught downstream)."""
        from selenium.webdriver.common.by import By
        selectors = (
            # Klaro (WBM's current provider, 2026-07)
            "#klaro .cm-btn.cm-btn-accept-all",
            "#klaro .cm-btn.cm-btn-success",
            "#klaro button[data-full-consent]",
            # Historical / cookie-action fallbacks seen on WBM
            "a.cookie-action[data-action='accept']",
            "#cn-accept-cookie",
        )
        for selector in selectors:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            for el in els:
                try:
                    if el.is_displayed():
                        el.click()
                        time.sleep(0.5)
                        return
                except Exception:
                    continue


class KleinanzeigenApplicator:
    """Send contact message on Kleinanzeigen listings.

    Requires kleinanzeigen_email + kleinanzeigen_password in applicant config.
    Logs in once per applicator instance and reuses the session across applies.
    """

    URL_MATCH = "kleinanzeigen.de"
    SITE_NAME = "Kleinanzeigen"
    LOGIN_URL = "https://www.kleinanzeigen.de/m-einloggen.html"
    # Sends via the logged-in account, not a per-message "from" address — so the
    # addy multi-email loop must NOT apply once per alias here (it would spam the
    # landlord the same message N times, or break login when an alias is used as
    # the username). AutoApplicator applies exactly once for account-based sites.
    ACCOUNT_BASED = True

    def __init__(self, applicant: dict, dry_run: bool = False, profile_dir: Optional[str] = None):
        self.applicant = applicant
        self.dry_run = dry_run
        self.profile_dir = profile_dir or None
        self._driver: Optional[Any] = None  # selenium WebDriver, typed loosely to avoid hard dep
        self._logged_in = False

    def apply(self, expose: dict) -> bool:
        url = expose.get("url", "")
        if self.URL_MATCH not in url:
            return False
        # kleinanzeigen_email falls back to applicant.email — most users use same address
        email = self.applicant.get("kleinanzeigen_email") or self.applicant.get("email", "")
        password = self.applicant.get("kleinanzeigen_password", "")
        if not email or not password:
            # User can't apply automatically without creds; treat as manual-apply
            # so the listing surfaces in the alert chain instead of silently dropping.
            raise ManualApplyRequired("Kleinanzeigen credentials not configured")
        try:
            driver = self._ensure_driver()
            if not self._logged_in:
                # Persistent profile may carry a still-valid session cookie —
                # check first to avoid an unnecessary login round-trip that
                # disturbs Kleinanzeigen's risk score (login forms attract
                # bot-detection scrutiny).
                if self._is_logged_in(driver):
                    self._logged_in = True
                    logger.info("Kleinanzeigen: existing session detected via "
                                "persistent profile — skipping login flow")
                elif not self._login(driver, email, password):
                    # Likely wrong password / account locked / 2FA prompt — needs human.
                    raise ManualApplyRequired("Kleinanzeigen login failed")
            return self._send_message(driver, url)
        except ManualApplyRequired:
            raise
        except Exception as exc:
            logger.warning("Kleinanzeigen application failed for %s: %s", url, exc)
            self._close_driver()
            return False

    def _ensure_driver(self):
        if self._driver is None:
            # Reuses persistent Chrome profile across applies. With a warm
            # session Kleinanzeigen will frequently skip its login wall
            # entirely (cookies still valid) — the auto-login dance becomes a
            # no-op rather than a fragile selector chain.
            self._driver = _chrome_driver(profile_dir=self.profile_dir)
        return self._driver

    def _is_logged_in(self, driver) -> bool:
        """Heuristic: a warm profile may already carry a Kleinanzeigen session
        cookie, in which case visiting the home page redirects us straight in
        and the login form is absent. We probe for the user-menu element that
        only renders for authenticated sessions."""
        from selenium.webdriver.common.by import By
        try:
            driver.get("https://www.kleinanzeigen.de/")
            els = driver.find_elements(By.CSS_SELECTOR,
                                       "[data-testid='user-menu'], #user-email, .user-account")
            return any(el.is_displayed() for el in els)
        except Exception:
            return False

    def _close_driver(self):
        if self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None
            self._logged_in = False

    def _login(self, driver, email: str, password: str) -> bool:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        try:
            driver.get(self.LOGIN_URL)
            wait = WebDriverWait(driver, 15)
            # Dismiss cookie banner if present
            try:
                driver.find_element(
                    By.CSS_SELECTOR,
                    "#gdpr-banner-accept, button[data-testid='gdpr-banner-accept']",
                ).click()
                time.sleep(1)
            except Exception:
                pass
            wait.until(EC.presence_of_element_located((By.ID, "login-email")))
            driver.find_element(By.ID, "login-email").send_keys(email)
            driver.find_element(By.ID, "login-password").send_keys(password)
            driver.find_element(By.ID, "login-submit").click()
            wait.until(EC.url_changes(self.LOGIN_URL))
            self._logged_in = True
            logger.info("Kleinanzeigen: logged in as %s", email)
            return True
        except Exception as exc:
            logger.warning("Kleinanzeigen login failed: %s", exc)
            return False

    def _send_message(self, driver, url: str) -> bool:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        driver.get(url)
        wait = WebDriverWait(driver, 10)
        try:
            wait.until(EC.element_to_be_clickable((
                By.CSS_SELECTOR,
                "button[data-testid='contact-button'], a[data-testid='contact-link'], "
                "button.contact-button, #contact-form-trigger, "
                "button[aria-label*='Nachricht'], a[href*='nachricht']",
            ))).click()
        except Exception:
            pass  # form may already be visible
        try:
            # Wait for the textarea instead of relying on a 1s sleep.
            WebDriverWait(driver, 5).until(EC.visibility_of_element_located((
                By.CSS_SELECTOR, "textarea[name='message'], textarea#message, textarea",
            )))
        except Exception:
            pass

        filled = _fill_field(driver,
                             "textarea[name='message'], textarea#message, textarea",
                             self.applicant.get("message", ""))
        if not filled:
            logger.warning(
                "Kleinanzeigen: message textarea not found on %s — selectors may be stale", url)
            return False
        if self.dry_run:
            logger.info("Kleinanzeigen dry-run: form filled but submit skipped for %s", url)
            return True
        try:
            driver.find_element(
                By.CSS_SELECTOR,
                "button[type='submit'][data-testid='contact-send-button'], "
                "button[type='submit'].contact-submit, "
                "button[type='submit']",
            ).click()
            time.sleep(2)
            logger.info("Kleinanzeigen message sent for %s", url)
            return True
        except Exception as exc:
            logger.warning("Kleinanzeigen could not submit form for %s: %s", url, exc)
            return False

    def close(self):
        """Quit the persistent Chrome driver. Idempotent."""
        self._close_driver()

    def __del__(self):
        self._close_driver()


class HowogeApplicator:
    """Submit Howoge "Anfrage senden" (Mietinteressent) form on a listing detail page.

    Howoge runs the TYPO3 ext:HowRealestate plugin. The contact form is plain
    HTML (no JS framework, no captcha as of 2026-05) and accepts a standard
    ``application/x-www-form-urlencoded`` POST — so we drive it with ``requests``
    rather than Selenium. The submission triggers a double-opt-in email; the
    user must click the confirmation link before Howoge actually registers
    interest. We treat a 200/302 response to the POST as success.

    Layout (verified live 2026-05-02): The detail URL embeds an ``obid``
    (e.g. ``1771-14536-9997``) which we lift out of the path. We then GET the
    application form at
    ``/immobiliensuche/wohnungssuche/besichtigung-vereinbaren/bewerbungsprozess.html``
    with that obid; the response contains a ``<form>`` whose action URL embeds
    a CSRF-style ``cHash`` token plus several ``__referrer`` / ``__trustedProperties``
    hidden fields. We harvest every hidden input verbatim and POST them back
    along with the user's firstName/lastName/email.
    """

    URL_MATCH = "howoge.de"
    SITE_NAME = "Howoge"

    _FORM_PATH = ("/immobiliensuche/wohnungssuche/besichtigung-vereinbaren/"
                  "bewerbungsprozess.html")
    _OBID_RE = re.compile(r"/detail/([0-9]+-[0-9]+-[0-9]+)")

    def __init__(self, applicant: dict, dry_run: bool = False, profile_dir: Optional[str] = None):
        self.applicant = applicant
        self.dry_run = dry_run
        # Howoge does not need a Chrome profile — kept in the signature so
        # AutoApplicator can construct every applicator with the same args.
        del profile_dir
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/126.0.0.0 Safari/537.36",
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
        })

    def close(self):
        try:
            self._session.close()
        except Exception:
            pass

    def apply(self, expose: dict) -> bool:
        url = expose.get("url", "")
        if self.URL_MATCH not in url:
            return False
        match = self._OBID_RE.search(url)
        if not match:
            logger.warning("Howoge: could not extract obid from %s", url)
            return False
        obid = match.group(1)

        form_url = (
            f"https://www.howoge.de{self._FORM_PATH}"
            f"?tx_howrealestate_visitform%5Baction%5D=showVisitForm"
            f"&tx_howrealestate_visitform%5Bcontroller%5D=Immoobject"
            f"&tx_howrealestate_visitform%5Bobid%5D={obid}"
        )
        try:
            resp = self._session.get(form_url, timeout=20)
        except requests.exceptions.RequestException as exc:
            logger.warning("Howoge: form GET failed for %s: %s", url, exc)
            return False
        if resp.status_code != 200:
            logger.warning("Howoge: form GET returned HTTP %d for %s",
                           resp.status_code, url)
            return False

        soup = BeautifulSoup(resp.content, "lxml")
        form = soup.find("form", id="show-visit-form")
        if not isinstance(form, Tag):
            logger.warning("Howoge: visit form not found on %s — page layout changed",
                           url)
            return False

        action = form.get("action") or ""
        if isinstance(action, list):
            action = action[0] if action else ""
        if not isinstance(action, str) or not action:
            logger.warning("Howoge: form action attribute missing on %s", url)
            return False
        action_url = urljoin(form_url, action)

        payload: dict[str, str] = {}
        for hidden in form.find_all("input", attrs={"type": "hidden"}):
            if not isinstance(hidden, Tag):
                continue
            name = hidden.get("name")
            if not isinstance(name, str) or not name:
                continue
            value = hidden.get("value", "")
            if isinstance(value, list):
                value = value[0] if value else ""
            payload[name] = value if isinstance(value, str) else ""

        last, first = WbmApplicator._split_name(self.applicant.get("name", ""))
        payload["tx_howrealestate_visitform[visitRequest][firstName]"] = first
        payload["tx_howrealestate_visitform[visitRequest][lastName]"] = (
            last or self.applicant.get("name", "")
        )
        payload["tx_howrealestate_visitform[visitRequest][email]"] = (
            self.applicant.get("email", "")
        )

        if not payload["tx_howrealestate_visitform[visitRequest][email]"]:
            logger.warning("Howoge: applicant email missing — cannot submit %s", url)
            return False

        if self.dry_run:
            logger.info("Howoge dry-run: form payload built (%d fields) for %s — submit skipped",
                        len(payload), url)
            return True

        try:
            post = self._session.post(action_url, data=payload, timeout=30,
                                      allow_redirects=True)
        except requests.exceptions.RequestException as exc:
            logger.warning("Howoge: submit POST failed for %s: %s", url, exc)
            return False
        if post.status_code >= 400:
            logger.warning("Howoge: submit POST returned HTTP %d for %s",
                           post.status_code, url)
            return False
        # The DOI flow renders a "Bitte bestätigen Sie Ihre E-Mail-Adresse"
        # confirmation page on success. We do not parse it — the 2xx is enough
        # to know the form was accepted; the user still has to click the email
        # link to actually finalise the interest registration.
        logger.info("Howoge: interest submitted for %s (DOI email sent to %s)",
                    url, payload["tx_howrealestate_visitform[visitRequest][email]"])
        return True


class AutoApplicator(Processor):
    """Auto-submit applications for Gewobag, WBM, and Kleinanzeigen exposes.

    When ``auto_apply.ollama_gate`` is true, consults Ollama before each submission;
    defaults to apply when Ollama is unreachable.

    Tracks per-site consecutive apply failures: once a site fails ``_STALE_THRESHOLD``
    times in a row on URLs that match its ``URL_MATCH`` (i.e. it should have worked),
    pushes a "selectors stale" alert through the optional ``alert_dispatch`` callback
    so the failure surfaces beyond a debug log. Counter resets on the next success.
    """

    def __init__(self, config, alert_dispatch: Optional[Callable[[list[str]], None]] = None,
                 store=None, alias_resolver=None, user_id: str = "default"):
        self.config = config
        applicant = config.applicant_config() if hasattr(config, "applicant_config") else {}
        # Base applicant data; the recipient email is swapped per-alias when an
        # AliasResolver yields more than one address for a landlord.
        self._base_applicant = dict(applicant)
        apply_cfg = getattr(config, "config", {}).get("auto_apply", {})
        dry_run = bool(apply_cfg.get("dry_run", False))
        # Fallback when the config predates per-source modes (e.g. a plain
        # YamlConfig in tests): the legacy single dry_run flag.
        self._default_dry_run = dry_run
        # Optional per-profile Store (send dedup) + AliasResolver (addy.io
        # multi-email). Both None => classic single-application behaviour.
        self._store = store
        self._alias_resolver = alias_resolver
        self._user_id = user_id
        # Profile-dir resolution: explicit config wins, env var second
        # (docker compose convention), empty string disables the feature.
        profile_dir = (apply_cfg.get("chrome_profile_dir", "")
                       or os.environ.get("BFH_CHROME_PROFILE", "")
                       or "").strip() or None
        self.applicators = [
            GewobagApplicator(applicant, dry_run=dry_run, profile_dir=profile_dir),
            WbmApplicator(applicant, dry_run=dry_run, profile_dir=profile_dir),
            HowogeApplicator(applicant, dry_run=dry_run, profile_dir=profile_dir),
            KleinanzeigenApplicator(applicant, dry_run=dry_run, profile_dir=profile_dir),
        ]
        # Application-questionnaire answers (WBS/income/salutation/consent). Only
        # WbmApplicator consumes them today; attach to all so the interface is
        # uniform and future appliers can use them.
        form_answers = config.form_answers() if hasattr(config, "form_answers") else {}
        for applicator in self.applicators:
            applicator.form_answers = form_answers
        self.gate = None
        if apply_cfg.get("ollama_gate", False):
            from berlin_flat_hunter.ollama_apply_gate import OllamaApplyGate
            self.gate = OllamaApplyGate(config)

        self._alert_dispatch = alert_dispatch
        self._failure_counts: dict[str, int] = {}
        self._last_stale_alert_ts: dict[str, float] = {}

    def process_expose(self, expose: dict) -> dict:
        # Per-source send mode: off = never apply, dry_run = fill but don't
        # submit, live = submit. Falls back to the legacy global dry_run flag
        # for configs that don't expose send_mode_for.
        crawler = expose.get("crawler", "") or ""
        if hasattr(self.config, "send_mode_for"):
            mode = self.config.send_mode_for(crawler)
        else:
            mode = "dry_run" if self._default_dry_run else "live"
        if mode == "off":
            return expose
        if self.gate is not None and not self.gate.should_apply(expose):
            logger.info("Ollama gate: skipping application for %s", expose.get("url"))
            return expose
        url = expose.get("url", "") or ""
        applicator = self._match(url)
        if applicator is None:
            return expose  # no site owns this URL (e.g. degewo/gesobau: notify-only)
        applicator.dry_run = (mode != "live")

        # One application PER selected email (real + any addy aliases). Without
        # an AliasResolver this is just the applicant's own email — the classic
        # single application. Store dedups per (listing, recipient) so a second
        # email isn't re-sent every cycle.
        key = self._listing_key(expose, crawler)
        emails = self._emails(crawler, key)
        # Account-based sites (Kleinanzeigen) send from the logged-in account, so
        # apply exactly once regardless of how many alias addresses are selected.
        if getattr(applicator, "ACCOUNT_BASED", False) and len(emails) > 1:
            logger.info("%s is account-based — applying once, ignoring %d extra alias(es)",
                        getattr(applicator, "SITE_NAME", "site"), len(emails) - 1)
            emails = emails[:1]
        for email in emails:
            if self._already_sent(key, email, mode):
                continue
            applicator.applicant = ({**self._base_applicant, "email": email}
                                    if email else self._base_applicant)
            try:
                ok = bool(applicator.apply(expose))
            except ManualApplyRequired as exc:
                # Site demands a human (reCAPTCHA, missing creds, …) — push a
                # per-listing alert so the user gets a ping with the URL to apply
                # by hand. Soft failure: don't tick the stale-selector counter.
                self._notify_manual_apply(applicator, expose, exc.reason)
                expose["manual_apply_required"] = True
                self._record_send(key, email, mode, applicator, ok=False,
                                  message=f"manual apply required: {exc.reason}")
                break
            except Exception as exc:
                site = getattr(applicator, "SITE_NAME", type(applicator).__name__)
                logger.warning("%s apply raised: %s", site, exc)
                ok = False
            self._record_send(key, email, mode, applicator, ok=ok)
            if ok:
                expose["applied"] = True
                self._record_success(applicator)
            else:
                self._record_failure(applicator, url)
        return expose

    def _match(self, url: str):
        """The single applicator whose URL_MATCH owns this URL (disjoint hosts)."""
        for applicator in self.applicators:
            url_match = getattr(applicator, "URL_MATCH", "")
            if url_match and url_match in url:
                return applicator
        return None

    @staticmethod
    def _listing_key(expose: dict, crawler: str) -> str:
        return f"{crawler or 'x'}:{expose.get('id', '')}"

    def _emails(self, source: str, listing_key: str) -> list[str]:
        base = self._base_applicant.get("email", "")
        if self._alias_resolver is not None:
            try:
                emails = self._alias_resolver.emails_for(source, base, listing_key)
                if emails:
                    return emails
            except Exception as exc:  # noqa: BLE001 — never let alias minting block an apply
                logger.warning("AliasResolver failed for %s: %s", source, exc)
        return [base] if base else [""]

    def _already_sent(self, listing_key: str, email: str, mode: str) -> bool:
        if self._store is None:
            return False
        if mode == "live":
            return self._store.has_live_send(self._user_id, listing_key, email)
        return self._store.has_send(self._user_id, listing_key, mode, email)

    def _record_send(self, listing_key: str, email: str, mode: str, applicator,
                     ok: bool, message: str = "") -> None:
        if self._store is None:
            return
        site = getattr(applicator, "SITE_NAME", type(applicator).__name__).lower()
        try:
            self._store.record_send(self._user_id, listing_key, mode=mode,
                                    channel=f"{site}-form", ok=ok, message=message,
                                    recipient=email or "")
        except Exception as exc:  # noqa: BLE001
            logger.warning("store.record_send failed: %s", exc)

    def _notify_manual_apply(self, applicator, expose: dict, reason: str):
        """Format a per-listing manual-apply notice and push it through the
        alert dispatch (which fans out to every configured notifier when
        ``monitoring.alert_via_notifiers: true``). Always logs even when no
        notifier is wired so the event is at least visible in the journal.
        """
        site = getattr(applicator, "SITE_NAME", type(applicator).__name__)
        title = (expose.get("title") or "").strip()
        url = expose.get("url", "")
        msg = (
            f"[MANUAL APPLY] {site}: cannot auto-submit ({reason}). "
            f"Apply by hand: {title} — {url}"
        ) if title else (
            f"[MANUAL APPLY] {site}: cannot auto-submit ({reason}). "
            f"Apply by hand: {url}"
        )
        logger.info(msg)
        if self._alert_dispatch is None:
            return
        try:
            self._alert_dispatch([msg])
        except Exception as exc:
            logger.warning("Manual-apply notify failed: %s", exc)

    def _record_success(self, applicator):
        site = getattr(applicator, "SITE_NAME", type(applicator).__name__)
        if self._failure_counts.get(site):
            logger.info("%s auto-apply recovered after %d failures",
                        site, self._failure_counts[site])
        self._failure_counts[site] = 0

    def _record_failure(self, applicator, url: str):
        site = getattr(applicator, "SITE_NAME", type(applicator).__name__)
        count = self._failure_counts.get(site, 0) + 1
        self._failure_counts[site] = count
        if count < _STALE_THRESHOLD:
            return
        now = time.time()
        if now - self._last_stale_alert_ts.get(site, 0.0) < _STALE_ALERT_COOLDOWN:
            return
        self._last_stale_alert_ts[site] = now
        msg = (
            f"[APPLICATOR ALERT] {site}: {count} consecutive auto-apply failures "
            f"(latest URL: {url}) — selectors likely stale, check applicator.py"
        )
        logger.error(msg)
        if self._alert_dispatch is None:
            return
        try:
            self._alert_dispatch([msg])
        except Exception as exc:
            logger.warning("AutoApplicator alert dispatch failed: %s", exc)

    def close(self):
        """Release any persistent resources (e.g. Kleinanzeigen Chrome driver)."""
        for applicator in self.applicators:
            try:
                applicator.close()
            except Exception:
                pass
