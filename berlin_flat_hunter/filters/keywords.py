"""Curated exclude-keyword lists + WBS detection, ported from the pi bot.

These power the ExcludeFilter (junk-listing shield) and WbsFilter. Kept here so
both filters and the profile.json adapter share one source of truth.
"""
import re

# Curated defaults covering the common "not a normal flat to rent" cases on
# Berlin Genossenschaft / Kleinanzeigen listings. Matched case-insensitively as
# substrings against title + description + address. High-precision on purpose.
DEFAULT_EXCLUDE_KEYWORDS: tuple[str, ...] = (
    # Senior / assisted living
    "seniorenwohnung", "senioren", "seniorengerecht", "betreutes wohnen",
    "service über sophia", "sophia",
    # Lease-takeover / temporary / sublet
    "nachmieter", "zwischenmiete", "zur untermiete", "untervermietung",
    "auf zeit", "ferienwohnung", "monteurzimmer", "monteurwohnung",
    "boardinghouse", "serviced apartment",
    # Flat swaps
    "wohnungstausch", "tauschwohnung",
    # Room in a shared flat
    "wg-zimmer",
    # Non-residential
    "ladenlokal", "ladengeschäft", "gewerbeeinheit", "gewerbefläche",
    "gewerberaum", "praxisraum", "büroraum",
)

# Checked against the TITLE only (they appear harmlessly in descriptions of
# normal flats, e.g. "nicht möbliert"). A keyword does NOT fire if one of its
# guard substrings is present (so "möbliert" never matches "unmöbliert").
DEFAULT_EXCLUDE_TITLE_KEYWORDS: tuple[str, ...] = ("möbliert",)
TITLE_EXCLUDE_GUARDS: dict[str, tuple[str, ...]] = {"möbliert": ("unmöbliert",)}

# Parking/garage-only listings: these words also occur in normal flats that
# include parking, so we only exclude when the listing has no rooms.
DEFAULT_EXCLUDE_PARKING_KEYWORDS: tuple[str, ...] = (
    "stellplatz", "garage", "carport", "duplexparker",
)


# --------------------------------------------------------------------------- #
# WBS (Wohnberechtigungsschein) detection.
# --------------------------------------------------------------------------- #
# A WBS is the social-housing eligibility certificate. Listings advertise it in
# many forms ("WBS erforderlich", "WBS 160", 'WBS "Rollstuhlfahrer"'). Some say
# it is explicitly NOT needed ("ohne WBS", "kein WBS", "WBS: nein", "kein WBS
# benötigt"). A "not required" phrase WINS over a bare mention, so e.g. "kein
# WBS benötigt" is never read as required.
_WBS_NOT_RE = re.compile(
    r"(ohne\s+wbs|kein(?:en)?\s+wbs|wbs\s*[:\-]?\s*(?:nein|nicht\s+erforderlich)|"
    r"keine?\s+wbs|wbs\s+nicht\s+(?:erforderlich|notwendig|benötigt)|"
    r"kein(?:en)?\s+wbs\s+(?:erforderlich|notwendig|benötigt))",
    re.IGNORECASE,
)
_WBS_RE = re.compile(r"\bwbs\b|wohnberechtigungsschein", re.IGNORECASE)


def wbs_required(*texts: str | None) -> bool | None:
    """True if a WBS is required, False if explicitly not required, None if not
    mentioned. A "not required" phrase (e.g. 'ohne WBS', 'kein WBS benötigt')
    wins over a bare WBS mention."""
    blob = " ".join(t for t in texts if t)
    if not blob:
        return None
    if _WBS_NOT_RE.search(blob):
        return False
    if _WBS_RE.search(blob):
        return True
    return None
