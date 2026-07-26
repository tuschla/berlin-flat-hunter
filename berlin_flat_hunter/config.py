"""BerlinConfig — extends flathunter's YamlConfig with Berlin-specific crawlers"""
from flathunter.config import YamlConfig
from flathunter.crawler.kleinanzeigen import Kleinanzeigen as _UpstreamKleinanzeigen

from berlin_flat_hunter.crawlers.degewo import Degewo
from berlin_flat_hunter.crawlers.gesobau import Gesobau
from berlin_flat_hunter.crawlers.gewobag import Gewobag
from berlin_flat_hunter.crawlers.howoge import Howoge
from berlin_flat_hunter.crawlers.kleinanzeigen import Kleinanzeigen
from berlin_flat_hunter.crawlers.wbm import Wbm


class BerlinConfig(YamlConfig):
    """YamlConfig subclass that adds Berlin public-housing crawlers and optional features"""

    def init_searchers(self):
        super().init_searchers()
        # Replace flathunter's upstream Kleinanzeigen crawler with our subclass
        # that captures the list-page description snippet (feeds ScamFilter).
        upstream = self.searchers()
        replaced = [Kleinanzeigen(self) if isinstance(s, _UpstreamKleinanzeigen) else s
                    for s in upstream]
        extra = [Gewobag(self), Wbm(self), Gesobau(self), Howoge(self), Degewo(self)]
        self.set_searchers(replaced + extra)

    def ollama_enabled(self) -> bool:
        cfg = self.config.get("ollama", {})
        return bool(cfg.get("enabled", False))

    def ollama_config(self) -> dict:
        return self.config.get("ollama", {})

    def auto_apply_enabled(self) -> bool:
        cfg = self.config.get("auto_apply", {})
        return bool(cfg.get("enabled", False))

    def applicant_config(self) -> dict:
        return self.config.get("applicant", {})

    def polygon_filter_enabled(self) -> bool:
        area = self.config.get("search_area", {})
        return bool(area.get("polygon"))

    def search_polygon(self) -> list:
        return self.config.get("search_area", {}).get("polygon", [])

    def plz_filter_enabled(self) -> bool:
        return bool(self.config.get("neighborhoods"))

    def neighborhood_plz(self) -> dict[str, list[str]]:
        """Map of name → list of Berlin postal codes (as strings).

        Configured under top-level ``neighborhoods:``. Postal codes may be
        given as ints or strings; they are normalised to 5-char strings.
        """
        raw = self.config.get("neighborhoods", {}) or {}
        out: dict[str, list[str]] = {}
        for name, codes in raw.items():
            if not isinstance(codes, (list, tuple)):
                continue
            out[name] = [str(c).zfill(5) for c in codes]
        return out

    def monitoring_config(self) -> dict:
        return self.config.get("monitoring", {})

    def stats_enabled(self) -> bool:
        return bool(self.config.get("statistics", {}).get("enabled", False))

    def stats_db_path(self) -> str:
        return self.config.get("statistics", {}).get("db_path", "")

    def scam_filter_enabled(self) -> bool:
        return bool(self.config.get("scam_filter", {}).get("enabled", False))

    def scam_filter_config(self) -> dict:
        return self.config.get("scam_filter", {}) or {}

    # ------------------------------------------------------------------
    # Per-source auto-apply mode. The legacy switch was a single
    # ``auto_apply.dry_run`` bool; now each source may declare its own mode
    # under ``auto_apply.send_modes`` while ``dry_run`` remains the fallback so
    # existing configs keep working unchanged.
    # ------------------------------------------------------------------
    def send_mode_for(self, source: str) -> str:
        """Return "off" | "dry_run" | "live" for ``source`` (crawler name).

        Precedence: an explicit per-source entry in ``auto_apply.send_modes``
        (case-insensitive on the source name) wins; otherwise fall back to the
        global mode implied by ``auto_apply.enabled`` + ``auto_apply.dry_run``.
        """
        cfg = self.config.get("auto_apply", {}) or {}
        modes = cfg.get("send_modes", {}) or {}
        # case-insensitive source lookup (config may key by "howoge" or "Howoge")
        for key, val in modes.items():
            if str(key).lower() == str(source).lower():
                mode = str(val).lower()
                return mode if mode in ("off", "dry_run", "live") else "off"
        if not cfg.get("enabled", False):
            return "off"
        return "dry_run" if cfg.get("dry_run", True) else "live"

    def email_alias_config(self) -> dict:
        return self.config.get("email_alias", {}) or {}

    def email_imap_config(self) -> dict:
        return self.config.get("email_imap", {}) or {}

    def form_answers(self) -> dict:
        """Answers to the unified application questionnaire (form_catalog keys)."""
        raw = self.config.get("form_answers", {}) or {}
        return {str(k): str(v) for k, v in raw.items()}

    def exclude_filter_config(self) -> dict:
        """Junk-listing exclude shield config. ``keywords`` falls back to the
        flathunter ``excluded_titles`` list so existing YAML profiles get
        description-wide matching too; ``use_defaults`` applies the curated
        shield (senior/sublet/swap/WG/commercial/furnished/parking)."""
        ex = self.config.get("exclude", {}) or {}
        keywords = ex.get("keywords")
        if keywords is None:
            keywords = self.config.get("excluded_titles", []) or []
        return {"keywords": list(keywords), "use_defaults": bool(ex.get("use_defaults", True))}

    def wbs_required_setting(self):
        """Profile's WBS setting: False = applicant has no WBS (drop WBS-only
        listings), True = has a WBS (keep all), None = not set (filter inactive)."""
        return (self.config.get("filters", {}) or {}).get("wbs_required")

    def state_db_path(self) -> str:
        """Per-profile SQLite for alias/send/imap dedup state. Defaults next to
        the main DB (alongside stats.db / schema_monitor.json)."""
        configured = self.config.get("state_db_path", "")
        if configured:
            return configured
        import os
        state_dir = os.path.dirname(self.database_location()) or "."
        return os.path.join(state_dir, "state.db")
