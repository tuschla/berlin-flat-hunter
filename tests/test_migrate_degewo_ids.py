"""scripts/migrate_degewo_ids.py: seed the new URL-derived Degewo ids so the
cutover re-notifies nothing."""
import os
import sqlite3
import sys

from flathunter.idmaintainer import IdMaintainer

from berlin_flat_hunter.crawlers.common import listing_id
from berlin_flat_hunter.stats import StatsLogger
from berlin_flat_hunter.store import Store

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import migrate_degewo_ids as mig  # noqa: E402

URL = "https://www.degewo.de/immosuche/details/3-zimmerwohnung-in-der-seniorenresidenz-koepenick"
NEW = listing_id(URL)
WBM_URL = "https://www.wbm.de/wohnungen-berlin/angebote/details/x/"


def _setup(tmp_path):
    os.makedirs(tmp_path / "jakob")
    idw = IdMaintainer(str(tmp_path / "jakob" / "db.sqlite"))
    # The same listing seen under two drifting bookmark uids, both notified.
    for uid in (320673099, 1333219804):
        idw.save_expose({"id": uid, "crawler": "Degewo", "url": URL, "title": "T"})
        idw.mark_processed(uid)
    idw.save_expose({"id": listing_id(WBM_URL), "crawler": "Wbm", "url": WBM_URL})
    idw.get_connection().close()

    store = Store(str(tmp_path / "jakob" / "state.db"))
    store.record_send("jakob", "Degewo:320673099", mode="dry_run", channel="degewo-form", ok=True)
    store.close()

    stats = StatsLogger(str(tmp_path / "stats.db"))
    stats.log({"id": 320673099, "crawler": "Degewo", "url": URL, "title": "T", "price": ""})
    stats.log({"id": 1333219804, "crawler": "Degewo", "url": URL, "title": "T", "price": "900 €"})
    stats.close()


def _snapshot(tmp_path):
    out = {}
    for rel in ("jakob/db.sqlite", "jakob/state.db", "stats.db"):
        con = sqlite3.connect(tmp_path / rel)
        out[rel] = [con.execute(f"SELECT * FROM {t}").fetchall()
                    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        con.close()
    return out


def test_dry_run_writes_nothing(tmp_path):
    _setup(tmp_path)
    before = _snapshot(tmp_path)
    assert mig.main(["--data-dir", str(tmp_path)]) == 0
    assert _snapshot(tmp_path) == before


def test_apply_seeds_rekeys_and_is_idempotent(tmp_path):
    _setup(tmp_path)
    assert mig.main(["--data-dir", str(tmp_path), "--apply"]) == 0

    idw = IdMaintainer(str(tmp_path / "jakob" / "db.sqlite"))
    assert idw.is_processed(NEW)  # the new id is already "seen": no re-notification
    con = idw.get_connection()
    assert con.execute("SELECT id FROM exposes WHERE crawler='Degewo'").fetchall() == [(NEW,)]
    assert con.execute("SELECT id FROM exposes WHERE crawler='Wbm'").fetchall() == \
        [(listing_id(WBM_URL),)]

    state = sqlite3.connect(tmp_path / "jakob" / "state.db")
    assert state.execute("SELECT listing_key FROM sends").fetchall() == [(f"Degewo:{NEW}",)]

    stats = sqlite3.connect(tmp_path / "stats.db")
    assert stats.execute("SELECT id, price FROM notices").fetchall() == [(NEW, "900 €")]

    after = _snapshot(tmp_path)
    assert mig.main(["--data-dir", str(tmp_path), "--apply"]) == 0
    assert _snapshot(tmp_path) == after
