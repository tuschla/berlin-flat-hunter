"""BerlinHunter — extends flathunter Hunter with polygon filter, Ollama filter, auto-apply,
and schema change monitoring."""
import os
import time
import traceback

import requests.exceptions
import urllib3.exceptions
from selenium.common.exceptions import WebDriverException

from flathunter.captcha.captcha_solver import CaptchaUnsolvableError
from flathunter.hunter import Hunter
from flathunter.filter import Filter
from flathunter.logging import logger
from flathunter.notifiers import SenderApprise, SenderMattermost, SenderSlack, SenderTelegram
from flathunter.processor import ProcessorChain
from flathunter.webdriver_crawler import WebdriverCrawler

from berlin_flat_hunter.applicator import AutoApplicator
from berlin_flat_hunter.config import BerlinConfig
from berlin_flat_hunter.monitoring.schema_monitor import SchemaMonitor
from berlin_flat_hunter.ollama_filter import OllamaFilter
from berlin_flat_hunter.stats import StatsLogger, StatsProcessor

_NOTIFIER_BUILDERS = {
    "apprise": SenderApprise,
    "telegram": SenderTelegram,
    "mattermost": SenderMattermost,
    "slack": SenderSlack,
}

# Don't tear down + rebuild a Selenium driver more often than this. A
# permanently broken site (e.g. wedged Cloudflare challenge) would otherwise
# churn Chrome processes once per cycle, hammering CPU + creating profile
# directory races. 5 minutes is well below the typical 5–10min hunt cadence
# while still preventing busy-loop recycles inside a single tight retry.
_DRIVER_RECYCLE_MIN_INTERVAL = 300.0  # seconds

# Exception classes that mean "the Selenium session is dead and the next
# crawl on this searcher will need a fresh driver". urllib3.exceptions.HTTPError
# covers MaxRetryError and ProtocolError raised by the chromedriver HTTP bridge
# when the underlying browser process has gone away. OSError catches stray
# pipe/socket errors that bubble through pyselenium's transport layer.
_DRIVER_WEDGE_EXCEPTIONS: tuple = (
    WebDriverException,
    urllib3.exceptions.HTTPError,
    OSError,
)


class BerlinHunter(Hunter):
    """Hunter subclass with polygon filtering, Ollama filtering, auto-application,
    and schema-change alerting.

    Stateful processors (AutoApplicator login session, PolygonFilter geocode cache)
    are constructed once and reused across hunt cycles.
    """

    def __init__(self, config: BerlinConfig, id_watch):
        super().__init__(config, id_watch)
        self.berlin_config = config

        state_dir = os.path.dirname(config.database_location()) or "."
        state_path = os.path.join(state_dir, "schema_monitor.json")
        self.schema_monitor = SchemaMonitor(state_path, config.config)

        # Stateful processors: build once, reuse across hunt cycles.
        self._polygon_filter = None
        if config.polygon_filter_enabled():
            from berlin_flat_hunter.filters.polygon_filter import PolygonFilter
            self._polygon_filter = PolygonFilter(config)

        self._plz_filter = None
        if config.plz_filter_enabled():
            from berlin_flat_hunter.filters.plz_filter import PlzFilter
            self._plz_filter = PlzFilter(config)

        self._ollama_filter = OllamaFilter(config) if config.ollama_enabled() else None

        # Build alert notifiers up-front so AutoApplicator (which receives
        # _dispatch_alerts as a callback) has a populated list ready by the
        # time it fires its first stale-selector or [MANUAL APPLY] alert.
        # `monitoring.alert_notifiers` overrides which notifiers fire for
        # alerts only — useful when per-expose notifications are off
        # (`notifiers: []`) but you still want Telegram pings on broken
        # crawlers / manual-apply situations.
        self._alert_notifiers: list = []
        mon_cfg = config.monitoring_config()
        if mon_cfg.get("alert_via_notifiers", False):
            alert_names = mon_cfg.get("alert_notifiers") or config.notifiers()
            for name in alert_names:
                builder = _NOTIFIER_BUILDERS.get(name)
                if builder is None:
                    logger.warning("SchemaMonitor: unknown notifier %r — skipping", name)
                    continue
                try:
                    self._alert_notifiers.append(builder(config))
                except Exception as exc:
                    logger.warning("SchemaMonitor: could not build %s notifier: %s", name, exc)

        self._auto_applicator = (
            AutoApplicator(config, alert_dispatch=self._dispatch_alerts)
            if config.auto_apply_enabled() else None
        )

        self._stats_processor = None
        if config.stats_enabled():
            stats_path = config.stats_db_path() or os.path.join(state_dir, "stats.db")
            self.stats = StatsLogger(stats_path)
            self._stats_processor = StatsProcessor(self.stats)
        else:
            self.stats = None

        # Per-searcher monotonic timestamp of the last driver tear-down so
        # _recycle_driver can rate-limit rebuilds on a permanently-broken site.
        self._driver_last_recycled: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Selenium driver lifecycle — recycling on wedge / dead session.
    # ------------------------------------------------------------------

    def crawl_for_exposes(self, max_pages=None):  # type: ignore[override]
        """Crawl every configured (searcher, url) pair, defensively.

        Upstream's ``Hunter.crawl_for_exposes`` only catches CaptchaUnsolvableError
        and requests.RequestException — Selenium failures (WebDriverException,
        urllib3.MaxRetryError) propagate out of ``searcher.crawl()``, escape
        ``hunt_flats``, and prevent the schema monitor from ever ticking the
        consecutive-empty counter for the wedged crawler. Worse, the bad
        ``searcher.driver`` reference stays on the searcher instance, so every
        subsequent cycle re-uses the dead session forever.

        We override here to catch driver wedges per (searcher, url), null the
        driver so the next ``get_driver()`` call rebuilds it, and continue with
        the remaining searchers. The dead crawler's failure surfaces in
        ``_record_health`` as an empty-crawl tick.
        """
        results: list[dict] = []
        # Pre-cycle health probe: a driver that died between cycles will
        # otherwise hit the same MaxRetryError on its first .crawl() call.
        # The probe is cheap (one HTTP round-trip to chromedriver) compared
        # to a full crawl that times out.
        for searcher in self.config.searchers():
            self._probe_and_recycle_if_dead(searcher)

        for searcher in self.config.searchers():
            name = searcher.get_name()
            for url in self.config.target_urls():
                # URL_PATTERN is a regex; use search to match anywhere in the URL
                # (consistent with how flathunter dispatches crawlers).
                if not searcher.URL_PATTERN.search(url):
                    continue
                try:
                    items = searcher.crawl(url, max_pages)
                except CaptchaUnsolvableError:
                    logger.info("%s: captcha unsolvable on %s", name, url)
                    continue
                except requests.exceptions.RequestException as exc:
                    logger.info("%s: request error on %s: %s", name, url, exc)
                    continue
                except _DRIVER_WEDGE_EXCEPTIONS as exc:
                    logger.warning(
                        "%s: driver wedged on %s (%s: %s) — recycling for next cycle",
                        name, url, type(exc).__name__, exc,
                    )
                    self._recycle_driver(searcher, force=True)
                    continue
                except Exception as exc:  # noqa: BLE001 — last-resort safety net
                    logger.warning(
                        "%s: unexpected crawl error on %s: %s: %s",
                        name, url, type(exc).__name__, exc,
                    )
                    continue
                if items:
                    results.extend(items)
        return iter(results)

    def _probe_and_recycle_if_dead(self, searcher):
        """Cheap liveness check: read driver.current_url. If chromedriver has
        died (browser crashed, connection lost) this throws immediately."""
        if not isinstance(searcher, WebdriverCrawler):
            return
        if searcher.driver is None:
            return
        try:
            _ = searcher.driver.current_url
        except _DRIVER_WEDGE_EXCEPTIONS as exc:
            logger.warning("%s: pre-crawl driver probe failed (%s) — recycling",
                           searcher.get_name(), type(exc).__name__)
            self._recycle_driver(searcher, force=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s: probe raised %s: %s — recycling",
                           searcher.get_name(), type(exc).__name__, exc)
            self._recycle_driver(searcher, force=False)

    def _recycle_driver(self, searcher, force: bool):
        """Tear down ``searcher.driver`` so the next ``get_driver()`` call
        rebuilds it. Bounded by ``_DRIVER_RECYCLE_MIN_INTERVAL`` unless
        ``force`` (i.e. we just hit a wedge — no point waiting)."""
        if not isinstance(searcher, WebdriverCrawler):
            return
        name = searcher.get_name()
        now = time.monotonic()
        last = self._driver_last_recycled.get(name, 0.0)
        if not force and (now - last) < _DRIVER_RECYCLE_MIN_INTERVAL:
            return
        try:
            if searcher.driver is not None:
                searcher.driver.quit()
        except Exception as exc:  # noqa: BLE001 — quit() on dead driver often raises
            logger.debug("%s: driver.quit() raised %s (ignored)",
                         name, type(exc).__name__)
        searcher.driver = None
        self._driver_last_recycled[name] = now

    # ------------------------------------------------------------------
    # Hunt cycle.
    # ------------------------------------------------------------------

    def hunt_flats(self, max_pages=None):
        try:
            return self._hunt_flats_inner(max_pages)
        except Exception:  # noqa: BLE001 — last-resort safety net
            # If the cycle dies before _record_health runs, the schema monitor
            # would otherwise stay silent forever. Force-tick every configured
            # crawler as empty so the consecutive-empty counter reflects reality.
            logger.error("Hunt cycle aborted by uncaught exception:\n%s",
                         traceback.format_exc())
            self._record_total_failure()
            return []

    def _hunt_flats_inner(self, max_pages):
        raw_exposes = list(self.crawl_for_exposes(max_pages))
        self._record_health(raw_exposes)

        filter_set = Filter.builder() \
                           .read_config(self.config) \
                           .filter_already_seen(self.id_watch) \
                           .build()

        builder = ProcessorChain.builder(self.config) \
                                .save_all_exposes(self.id_watch) \
                                .apply_filter(filter_set) \
                                .resolve_addresses() \
                                .calculate_durations()

        if self._stats_processor is not None:
            builder.processors.append(self._stats_processor)

        if self._plz_filter is not None:
            builder.processors.append(self._plz_filter)
        if self._polygon_filter is not None:
            builder.processors.append(self._polygon_filter)
        if self._ollama_filter is not None:
            builder.processors.append(self._ollama_filter)

        builder = builder.send_messages()

        if self._auto_applicator is not None:
            builder.processors.append(self._auto_applicator)

        result = []
        for expose in builder.build().process(raw_exposes):
            logger.info("New offer: %s", expose["title"])
            result.append(expose)
        return result

    def _record_health(self, raw_exposes: list[dict]):
        """Update the schema monitor with per-crawler crawl results.

        A crawler is only health-tracked if at least one configured URL matches
        its URL_PATTERN — otherwise an unused crawler would be flagged as broken.
        Any alerts raised are pushed through configured notifiers.
        """
        alerts = list(self.schema_monitor.record_crawl(raw_exposes))
        seen = {e.get("crawler") for e in raw_exposes}
        for name in self._configured_crawler_names():
            if name in seen:
                continue
            alerts.extend(self.schema_monitor.record_empty_crawl(name))
        self._dispatch_alerts(alerts)

    def _record_total_failure(self):
        """Tick consecutive_empty for every configured crawler when the whole
        hunt cycle aborts before ``_record_health`` could run. Without this,
        a Selenium wedge that escapes the per-searcher catch in
        ``crawl_for_exposes`` would leave the schema monitor's counter frozen
        and no alert would ever fire."""
        alerts: list[str] = []
        for name in self._configured_crawler_names():
            alerts.extend(self.schema_monitor.record_empty_crawl(name))
        self._dispatch_alerts(alerts)

    def _configured_crawler_names(self) -> list[str]:
        """Names of searchers whose URL_PATTERN matches at least one configured
        target URL. Centralised so _record_health and _record_total_failure
        agree on which crawlers count as 'configured-and-expected-to-work'."""
        target_urls = self.config.target_urls()
        names: list[str] = []
        for searcher in self.config.searchers():
            if any(searcher.URL_PATTERN.match(url) for url in target_urls):
                names.append(searcher.get_name())
        return names

    def _dispatch_alerts(self, alerts: list[str]):
        """Send schema alerts through any configured notifiers."""
        if not alerts or not self._alert_notifiers:
            return
        for msg in alerts:
            for notifier in self._alert_notifiers:
                try:
                    notifier.notify(msg)
                except Exception as exc:
                    logger.warning("Failed to push alert via %s: %s",
                                   type(notifier).__name__, exc)

    def close(self):
        """Release stats DB and any applicator drivers. Idempotent and safe even
        if __init__ did not finish (uses getattr defaults).
        """
        applicator = getattr(self, "_auto_applicator", None)
        if applicator is not None:
            try:
                applicator.close()
            except Exception as exc:
                logger.warning("AutoApplicator close failed: %s", exc)
        stats = getattr(self, "stats", None)
        if stats is not None:
            try:
                stats.close()
            except Exception as exc:
                logger.warning("Stats close failed: %s", exc)
