"""BerlinHunter — extends flathunter Hunter with polygon filter, Ollama filter, auto-apply,
and schema change monitoring."""
import os

from flathunter.hunter import Hunter
from flathunter.filter import Filter
from flathunter.logging import logger
from flathunter.notifiers import SenderApprise, SenderMattermost, SenderSlack, SenderTelegram
from flathunter.processor import ProcessorChain

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
        self._auto_applicator = AutoApplicator(config) if config.auto_apply_enabled() else None

        self._stats_processor = None
        if config.stats_enabled():
            stats_path = config.stats_db_path() or os.path.join(state_dir, "stats.db")
            self.stats = StatsLogger(stats_path)
            self._stats_processor = StatsProcessor(self.stats)
        else:
            self.stats = None

        # Build alert notifiers if monitoring.alert_via_notifiers is true.
        # Reuses the same notifier list from `notifiers:` so users don't reconfigure.
        self._alert_notifiers: list = []
        mon_cfg = config.monitoring_config()
        if mon_cfg.get("alert_via_notifiers", False):
            for name in config.notifiers():
                builder = _NOTIFIER_BUILDERS.get(name)
                if builder is None:
                    logger.warning("SchemaMonitor: unknown notifier %r — skipping", name)
                    continue
                try:
                    self._alert_notifiers.append(builder(config))
                except Exception as exc:
                    logger.warning("SchemaMonitor: could not build %s notifier: %s", name, exc)

    def hunt_flats(self, max_pages=None):
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
        target_urls = self.config.target_urls()
        for searcher in self.config.searchers():
            name = searcher.get_name()
            if name in seen:
                continue
            # Skip crawlers with no matching URLs — they aren't broken, just unused.
            if not any(searcher.URL_PATTERN.match(url) for url in target_urls):
                continue
            alerts.extend(self.schema_monitor.record_empty_crawl(name))
        self._dispatch_alerts(alerts)

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
