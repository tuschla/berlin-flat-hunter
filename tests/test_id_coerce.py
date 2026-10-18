"""Listing identity: crawlers derive ids from the detail URL (crawlers.common.listing_id)."""
import zlib

import pytest

from berlin_flat_hunter.crawlers.common import listing_id


# (url, id stored live in stats.db / the profiles' `processed` tables). The
# derivation must never change, or every live listing notifies again.
LIVE_IDS = [
    ("https://www.gewobag.de/fuer-mietinteressentinnen/mietangebote/0100-02101-0302-0120/",
     3153038178),
    ("https://www.howoge.de/immobiliensuche/wohnungssuche/detail/1771-12738-41.html",
     1658554473),
    ("https://www.wbm.de/wohnungen-berlin/angebote/details/3-zim-wohnung-in-kreuzberg/",
     3409408620),
]


@pytest.mark.parametrize("url,stored", LIVE_IDS)
def test_matches_ids_already_stored(url, stored):
    assert listing_id(url) == stored


def test_trailing_slash_irrelevant():
    url = "https://www.degewo.de/immosuche/details/nachmieter-gesucht-30"
    assert listing_id(url) == listing_id(url + "/") == zlib.crc32(b"nachmieter-gesucht-30")


def test_query_style_detail_urls_stay_distinct():
    base = "https://www.degewo.de/immosuche/details?tx_openimmo_immobilie%5Bimmobilie%5D="
    assert listing_id(base + "2940") != listing_id(base + "2941")


def test_id_fits_flathunter_integer_columns():
    i = listing_id("https://www.wbm.de/x/some-flat/")
    assert isinstance(i, int) and 0 <= i < 2 ** 32
