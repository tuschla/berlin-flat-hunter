"""SchemaMonitor — detect when a crawler's schema has broken and alert"""
import json
import os
import time
from typing import Optional

from flathunter.logging import logger

CRITICAL_FIELDS = ("title", "url", "address", "price")


class CrawlerHealth:
    __slots__ = ("consecutive_empty", "last_success_ts", "last_alert_ts")

    def __init__(self):
        self.consecutive_empty: int = 0
        self.last_success_ts: float = 0.0
        self.last_alert_ts: float = 0.0

    def to_dict(self) -> dict:
        return {
            "consecutive_empty": self.consecutive_empty,
            "last_success_ts": self.last_success_ts,
            "last_alert_ts": self.last_alert_ts,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CrawlerHealth":
        h = cls()
        h.consecutive_empty = d.get("consecutive_empty", 0)
        h.last_success_ts = d.get("last_success_ts", 0.0)
        h.last_alert_ts = d.get("last_alert_ts", 0.0)
        return h


class SchemaMonitor:
    """Tracks per-crawler health and fires alerts when things look broken.

    State persists to a JSON file so consecutive-empty counts survive restarts.

    Alert conditions (both configurable in YAML under `monitoring`):
      - consecutive_empty_threshold: alert after N runs with 0 results (default 3)
      - field_miss_threshold: alert if >X fraction of results missing critical fields (default 0.5)
    Alert cool-down: 1 hour between repeated alerts for the same crawler.
    """

    _ALERT_COOLDOWN = 3600  # seconds

    def __init__(self, state_path: str, config: Optional[dict] = None):
        self.state_path = state_path
        cfg = (config or {}).get("monitoring", {})
        self.empty_threshold: int = cfg.get("consecutive_empty_threshold", 3)
        self.field_miss_threshold: float = cfg.get("field_miss_threshold", 0.5)
        self._health: dict[str, CrawlerHealth] = {}
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_crawl(self, exposes: list[dict]) -> list[str]:
        """Update health state for all crawlers represented in exposes.

        Returns list of alert messages (empty if healthy).
        """
        by_crawler: dict[str, list[dict]] = {}
        for expose in exposes:
            name = expose.get("crawler", "unknown")
            by_crawler.setdefault(name, []).append(expose)

        alerts: list[str] = []
        for crawler_name, crawler_exposes in by_crawler.items():
            health = self._get_health(crawler_name)
            msgs = self._evaluate(crawler_name, crawler_exposes, health)
            alerts.extend(msgs)

        self._save()
        return alerts

    def record_empty_crawl(self, crawler_name: str) -> list[str]:
        """Call this when a crawler returns 0 results."""
        health = self._get_health(crawler_name)
        health.consecutive_empty += 1
        alerts = self._maybe_alert_empty(crawler_name, health)
        self._save()
        return alerts

    def get_health_summary(self) -> dict:
        return {name: h.to_dict() for name, h in self._health.items()}

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_health(self, name: str) -> CrawlerHealth:
        if name not in self._health:
            self._health[name] = CrawlerHealth()
        return self._health[name]

    def _evaluate(self, name: str, exposes: list[dict], health: CrawlerHealth) -> list[str]:
        # _evaluate is only called for crawlers with ≥1 expose; empty runs go through
        # record_empty_crawl. Reset the empty counter and check field completeness.
        health.consecutive_empty = 0
        health.last_success_ts = time.time()

        miss_count = sum(
            1 for e in exposes
            if any(not e.get(f) for f in CRITICAL_FIELDS)
        )
        miss_rate = miss_count / len(exposes)
        if miss_rate < self.field_miss_threshold:
            return []
        msg = (
            f"[SCHEMA ALERT] {name}: {miss_count}/{len(exposes)} exposes have empty "
            f"critical fields ({miss_rate:.0%}) — site schema may have changed"
        )
        return self._maybe_send_alert(name, health, msg)

    def _maybe_alert_empty(self, name: str, health: CrawlerHealth) -> list[str]:
        if health.consecutive_empty >= self.empty_threshold:
            msg = (
                f"[SCHEMA ALERT] {name}: returned 0 results for "
                f"{health.consecutive_empty} consecutive runs — "
                f"site may be down or schema changed"
            )
            return self._maybe_send_alert(name, health, msg)
        return []

    def _maybe_send_alert(self, name: str, health: CrawlerHealth, msg: str) -> list[str]:
        now = time.time()
        if now - health.last_alert_ts < self._ALERT_COOLDOWN:
            return []  # still in cool-down
        health.last_alert_ts = now
        logger.error(msg)
        return [msg]

    def _load(self):
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path) as f:
                data = json.load(f)
            for name, d in data.items():
                self._health[name] = CrawlerHealth.from_dict(d)
        except Exception as exc:
            logger.warning("SchemaMonitor: could not load state from %s: %s", self.state_path, exc)

    def _save(self):
        # Atomic write: write to a sibling tempfile, then rename. Prevents
        # corruption if the process is killed mid-write.
        tmp_path = f"{self.state_path}.tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump({k: v.to_dict() for k, v in self._health.items()}, f, indent=2)
            os.replace(tmp_path, self.state_path)
        except Exception as exc:
            logger.warning("SchemaMonitor: could not save state to %s: %s", self.state_path, exc)
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
