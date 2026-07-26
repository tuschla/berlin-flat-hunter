"""Shared-scrape orchestrator.

The single most important property of this module: **each source is crawled once
per cycle, no matter how many search profiles are active.** The old deployment
ran one full flathunter process per profile (``bfh-single``, ``bfh-wg``), so N
profiles meant N full crawls of the very same public-housing sites every few
minutes. Here a single *lead* scraper crawls the UNION of every profile's target
URLs exactly once, records crawl health on one shared schema monitor, and then
fans the identical raw exposes out to each profile's own filter → notify → apply
pipeline (:meth:`BerlinHunter.process_raw`).

Everything downstream of the crawl stays per-profile: each profile keeps its own
``IdMaintainer`` (so "already seen" / notification dedup is independent), its own
filters, notifiers, stats DB and auto-applicator. Only the network-heavy scrape
is shared.

Config lives in ``hunter.yaml`` (see ``hunter.yaml.example``):

    global:
      database_location: /path/to/data/db.sqlite   # shared crawl state lives here
      loop: {active: true, sleeping_time: 300}
      monitoring: {...}          # schema-change / crawler-down alerting
      notifiers: [telegram]      # channel for crawler-down alerts
      telegram: {bot_token: ..., receiver_ids: [...]}
    profiles:
      - profiles/single.yaml
      - profiles/wg.yaml
"""
import os
import time
import traceback

import yaml
from flathunter.idmaintainer import IdMaintainer
from flathunter.logging import logger

from berlin_flat_hunter.config import BerlinConfig
from berlin_flat_hunter.hunter import BerlinHunter


def _resolve(path: str, base_dir: str) -> str:
    """Resolve a (possibly relative) profile path against the hunter.yaml dir."""
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(base_dir, path))


def _load_profile_hunter(path: str) -> tuple[str, BerlinHunter]:
    """Build one (name, BerlinHunter) from a profile YAML. The name is the
    profile file's basename without extension (e.g. ``single``)."""
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    config = BerlinConfig(raw)
    config.init_searchers()
    id_watch = IdMaintainer(config.database_location())
    hunter = BerlinHunter(config, id_watch)
    name = os.path.splitext(os.path.basename(path))[0]
    return name, hunter


class Orchestrator:
    """Owns the shared crawl + one BerlinHunter per profile."""

    def __init__(self, hunter_cfg: dict, base_dir: str = "."):
        self.base_dir = base_dir
        g = hunter_cfg.get("global", {}) or {}
        self._global = g

        loop_cfg = g.get("loop", {}) or {}
        self.loop_active = bool(loop_cfg.get("active", True))
        self.loop_period = int(loop_cfg.get("sleeping_time", 300))

        profile_paths = hunter_cfg.get("profiles", []) or []
        if not profile_paths:
            raise ValueError("hunter.yaml lists no profiles under `profiles:`")
        self.profiles: list[tuple[str, BerlinHunter]] = [
            _load_profile_hunter(_resolve(p, base_dir)) for p in profile_paths
        ]
        logger.info("Orchestrator: loaded %d profile(s): %s",
                    len(self.profiles), ", ".join(n for n, _ in self.profiles))

        self.lead = self._build_lead(g)
        union = self.lead.config.target_urls()
        logger.info("Orchestrator: shared crawl over %d unique URL(s)", len(union))

    # ------------------------------------------------------------------
    def _build_lead(self, g: dict) -> BerlinHunter:
        """A crawl-only BerlinHunter over the UNION of all profiles' URLs.

        It carries the shared SchemaMonitor (at ``<data>/schema_monitor.json``)
        and, if ``global.monitoring.alert_via_notifiers`` is set, the crawler-down
        alert notifiers. It never processes exposes, so it builds no filters,
        stats or applicator.
        """
        union_urls: list[str] = []
        seen: set[str] = set()
        # Extra shared URLs declared at the orchestrator level (e.g. a source no
        # single profile lists yet, like degewo) are crawled once and offered to
        # every profile's filter chain just like any other source.
        url_sources = [g.get("urls", []) or []] + [h.config.target_urls() for _, h in self.profiles]
        for urls in url_sources:
            for url in urls:
                if url not in seen:
                    seen.add(url)
                    union_urls.append(url)

        default_db = os.path.join(self.base_dir, "data", "db.sqlite")
        lead_dict = {
            "database_location": g.get("database_location", default_db),
            "urls": union_urls,
            "monitoring": g.get("monitoring", {}) or {},
            "notifiers": g.get("notifiers", []) or [],
        }
        if g.get("telegram"):
            lead_dict["telegram"] = g["telegram"]
        lead_config = BerlinConfig(lead_dict)
        lead_config.init_searchers()
        lead_id = IdMaintainer(lead_config.database_location())
        return BerlinHunter(lead_config, lead_id)

    # ------------------------------------------------------------------
    @staticmethod
    def _dedup(raw_exposes: list[dict]) -> list[dict]:
        """Collapse duplicates from overlapping profile URLs.

        Public-housing URLs are identical across profiles and Kleinanzeigen
        pre-filter URLs overlap (a ≤600€ flat shows up under both the ≤600 and
        the ≤1100 search), so the union crawl can surface the same listing twice.
        Dedup by URL (the natural unique key) before fan-out so no profile
        double-processes a listing in a single cycle.
        """
        out: list[dict] = []
        seen: set = set()
        for expose in raw_exposes:
            key = expose.get("url") or (expose.get("crawler"), expose.get("id"))
            if key in seen:
                continue
            seen.add(key)
            out.append(expose)
        return out

    def run_once(self, max_pages=None) -> None:
        """One cycle: shared crawl → health → per-profile fan-out."""
        raw = self._dedup(list(self.lead.crawl_for_exposes(max_pages)))
        self.lead._record_health(raw)  # one schema-monitor tick for the shared crawl
        logger.info("Shared crawl: %d unique expose(s); fanning out to %d profile(s)",
                    len(raw), len(self.profiles))
        for name, hunter in self.profiles:
            try:
                results = hunter.process_raw(raw)
                logger.info("Profile %s: %d new offer(s)", name, len(results))
            except Exception:  # noqa: BLE001 — one bad profile must not sink the cycle
                logger.error("Profile %s processing failed:\n%s", name, traceback.format_exc())

    def loop(self) -> None:
        if not self.loop_active:
            self.run_once()
            return
        logger.info("Orchestrator loop active (every %ds)", self.loop_period)
        while True:
            try:
                self.run_once()
            except Exception:  # noqa: BLE001
                logger.error("Cycle failed:\n%s", traceback.format_exc())
            time.sleep(self.loop_period)

    def close(self) -> None:
        for name, hunter in self.profiles:
            try:
                hunter.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Closing profile %s failed: %s", name, exc)
        try:
            self.lead.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Closing lead scraper failed: %s", exc)

    # ------------------------------------------------------------------
    @classmethod
    def from_file(cls, path: str) -> "Orchestrator":
        with open(path) as f:
            hunter_cfg = yaml.safe_load(f) or {}
        return cls(hunter_cfg, base_dir=os.path.dirname(os.path.abspath(path)))
