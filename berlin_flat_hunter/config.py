"""BerlinConfig — extends flathunter's YamlConfig with Berlin-specific crawlers"""
from flathunter.config import YamlConfig

from berlin_flat_hunter.crawlers.gesobau import Gesobau
from berlin_flat_hunter.crawlers.gewobag import Gewobag
from berlin_flat_hunter.crawlers.wbm import Wbm


class BerlinConfig(YamlConfig):
    """YamlConfig subclass that adds Berlin public-housing crawlers and optional features"""

    def init_searchers(self):
        super().init_searchers()
        extra = [Gewobag(self), Wbm(self), Gesobau(self)]
        self.set_searchers(self.searchers() + extra)

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
