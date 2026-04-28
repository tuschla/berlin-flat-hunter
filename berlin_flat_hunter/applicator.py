"""AutoApplicator — Selenium-based form submission for Gewobag, WBM, and Kleinanzeigen.

Caveat: form structures change frequently. Selectors verified against live sites
on 2026-04-26. See README.md for current status. Use ``dry_run: true`` in config
to fill forms without submitting (recommended on first use).
"""
import time
from typing import Any, Optional

from flathunter.abstract_processor import Processor
from flathunter.logging import logger


def _chrome_driver(headless: bool = True):
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
    """Try each CSS selector in a comma-separated list; fill first match. Returns True if filled."""
    if not value:
        return False
    for raw in selector.split(","):
        sel = raw.strip()
        if not sel:
            continue
        try:
            el = driver.find_element("css selector", sel)
            el.clear()
            el.send_keys(value)
            return True
        except Exception:
            continue
    return False


_COMMON_SELECTORS = {
    "name": "input[name*='name'], input[id*='name']",
    "email": "input[type='email'], input[name*='email'], input[id*='email']",
    "phone": "input[type='tel'], input[name*='phone'], input[id*='phone']",
    "message": "textarea",
    "submit": "button[type='submit'], input[type='submit']",
}


class GewobagApplicator:
    """Submit Anfrage form on a Gewobag listing detail page.

    The contact form is JS-rendered behind an "Anfrage senden" button; we click
    it first, then fill the (hopefully) revealed form. Layout has historically
    been brittle — set ``auto_apply.dry_run: true`` to verify before going live.
    """

    URL_MATCH = "gewobag.de"
    SITE_NAME = "Gewobag"

    def __init__(self, applicant: dict, dry_run: bool = False):
        self.applicant = applicant
        self.dry_run = dry_run

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

            driver = _chrome_driver()
            try:
                driver.get(url)
                wait = WebDriverWait(driver, 10)
                # Click "Anfrage senden" tab/button to reveal the contact form
                try:
                    wait.until(EC.element_to_be_clickable((
                        By.CSS_SELECTOR,
                        "button.rental-contact, button[data-tab='rental-contact'], "
                        "a[href*='anfrage'], a.rental-contact",
                    ))).click()
                    # Wait for any input the form should expose, instead of a fixed sleep —
                    # form rendering may take longer than 1s on slow connections.
                    try:
                        WebDriverWait(driver, 5).until(EC.visibility_of_element_located((
                            By.CSS_SELECTOR, "input[type='email'], textarea",
                        )))
                    except Exception:
                        pass
                except Exception:
                    logger.warning("Gewobag: 'Anfrage senden' button not found on %s", url)
                # Fill whatever inputs the now-rendered form exposes
                filled = sum([
                    _fill_field(driver, _COMMON_SELECTORS["name"], self.applicant.get("name", "")),
                    _fill_field(driver, _COMMON_SELECTORS["email"], self.applicant.get("email", "")),
                    _fill_field(driver, _COMMON_SELECTORS["phone"], self.applicant.get("phone", "")),
                    _fill_field(driver, _COMMON_SELECTORS["message"], self.applicant.get("message", "")),
                ])
                if filled == 0:
                    logger.warning("Gewobag: no form fields matched on %s — selectors may be stale", url)
                    return False
                if self.dry_run:
                    logger.info("Gewobag dry-run: %d fields filled but submit skipped for %s",
                                filled, url)
                    return True
                driver.find_element(By.CSS_SELECTOR, _COMMON_SELECTORS["submit"]).click()
                time.sleep(2)
                logger.info("Gewobag application submitted for %s", url)
                return True
            finally:
                driver.quit()
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

    def __init__(self, applicant: dict, dry_run: bool = False):
        self.applicant = applicant
        self.dry_run = dry_run

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

            driver = _chrome_driver()
            try:
                driver.get(url)
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "form.powermail_form"))
                )
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


class KleinanzeigenApplicator:
    """Send contact message on Kleinanzeigen listings.

    Requires kleinanzeigen_email + kleinanzeigen_password in applicant config.
    Logs in once per applicator instance and reuses the session across applies.
    """

    URL_MATCH = "kleinanzeigen.de"
    LOGIN_URL = "https://www.kleinanzeigen.de/m-einloggen.html"

    def __init__(self, applicant: dict, dry_run: bool = False):
        self.applicant = applicant
        self.dry_run = dry_run
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
            logger.warning("KleinanzeigenApplicator: email and kleinanzeigen_password required")
            return False
        try:
            driver = self._ensure_driver()
            if not self._logged_in and not self._login(driver, email, password):
                return False
            return self._send_message(driver, url)
        except Exception as exc:
            logger.warning("Kleinanzeigen application failed for %s: %s", url, exc)
            self._close_driver()
            return False

    def _ensure_driver(self):
        if self._driver is None:
            self._driver = _chrome_driver()
        return self._driver

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


class AutoApplicator(Processor):
    """Auto-submit applications for Gewobag, WBM, and Kleinanzeigen exposes.

    When `auto_apply.ollama_gate` is true, consults Ollama before each submission;
    defaults to apply when Ollama is unreachable.
    """

    def __init__(self, config):
        self.config = config
        applicant = config.applicant_config() if hasattr(config, "applicant_config") else {}
        apply_cfg = getattr(config, "config", {}).get("auto_apply", {})
        dry_run = bool(apply_cfg.get("dry_run", False))
        self.applicators = [
            GewobagApplicator(applicant, dry_run=dry_run),
            WbmApplicator(applicant, dry_run=dry_run),
            KleinanzeigenApplicator(applicant, dry_run=dry_run),
        ]
        self.gate = None
        if apply_cfg.get("ollama_gate", False):
            from berlin_flat_hunter.ollama_apply_gate import OllamaApplyGate
            self.gate = OllamaApplyGate(config)

    def process_expose(self, expose: dict) -> dict:
        if self.gate is not None and not self.gate.should_apply(expose):
            logger.info("Ollama gate: skipping application for %s", expose.get("url"))
            return expose
        for applicator in self.applicators:
            if applicator.apply(expose):
                expose["applied"] = True
                break
        return expose

    def close(self):
        """Release any persistent resources (e.g. Kleinanzeigen Chrome driver)."""
        for applicator in self.applicators:
            try:
                applicator.close()
            except Exception:
                pass
