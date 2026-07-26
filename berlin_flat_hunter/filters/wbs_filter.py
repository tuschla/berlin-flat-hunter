"""WbsFilter — drop WBS-restricted listings for applicants without a WBS.

A Wohnberechtigungsschein (WBS) is a social-housing eligibility certificate; a
large share of Berlin public-housing listings require one. An applicant who
doesn't hold a WBS can't rent those flats, so notifying/auto-applying to them is
noise. This filter drops a listing when its text says a WBS IS required — but
NEVER when it says a WBS is NOT needed ("ohne WBS", "kein WBS benötigt", …),
which the WBS detector treats as a hard negation.

Driven by the profile's ``filters.wbs_required``:
- ``false`` → applicant has no WBS → drop WBS-required listings (this filter runs).
- ``true``  → applicant has a WBS → keep everything (filter inactive).
- absent    → filter inactive (unchanged behaviour), so it never surprises an
  existing YAML profile that didn't opt in.
"""
from typing import Iterator

from flathunter.abstract_processor import Processor
from flathunter.logging import logger

from berlin_flat_hunter.filters.keywords import wbs_required


class WbsFilter(Processor):
    def __init__(self, config):
        setting = config.wbs_required_setting() if hasattr(config, "wbs_required_setting") else None
        # Active only when the applicant explicitly has NO WBS (False).
        self.active = setting is False

    @staticmethod
    def enabled_for(config) -> bool:
        setting = config.wbs_required_setting() if hasattr(config, "wbs_required_setting") else None
        return setting is False

    def process_exposes(self, exposes) -> Iterator[dict]:  # type: ignore[override]
        for expose in exposes:
            if self.active and wbs_required(
                expose.get("title", ""), expose.get("description", ""),
                expose.get("address", ""),
            ) is True:
                logger.info("WbsFilter: dropping WBS-required listing %s", expose.get("url"))
                continue
            yield expose
