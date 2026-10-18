#!/usr/bin/env python3
"""One-time cutover: re-key Degewo listings to URL-derived ids.

Degewo ids used to be the site's bookmark uid, which changes for the same
listing. Every crawler now derives its ids from the detail URL
(``berlin_flat_hunter.crawlers.common.listing_id``). Without this migration
every Degewo listing still online would notify (and, with auto-apply on,
apply) once more under its new id. The other sources already have URL-derived
ids (Aug 29 migration) and are not touched.

Under ``--data-dir`` (default ``/data``, the app's state dataset):

* ``db.sqlite`` and ``*/db.sqlite`` (flathunter's per-profile "already seen" DB):
  every Degewo row in ``exposes`` gets its new id added to ``processed``, and
  the ``exposes`` rows are re-keyed (newest row per listing wins).
* ``*/state.db`` (auto-apply dedup): ``sends.listing_key`` ``Degewo:<old>`` and
  ``aliases.scope_key`` ``listing:Degewo:<old>`` are rewritten to the new id.
* ``stats.db`` (general-data log): Degewo ``notices`` are re-keyed; rows of the
  same listing are merged (earliest first sighting, latest re-sighting, first
  non-empty value per column).

Run it with the hunter stopped, before the new image's first cycle:

    python scripts/migrate_degewo_ids.py            # dry run: print what would change
    python scripts/migrate_degewo_ids.py --apply    # write

A second run finds nothing left to change.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys

from berlin_flat_hunter.crawlers.common import listing_id

CRAWLER = "Degewo"
_STATS_CONTENT = ("url", "title", "address", "rooms", "size", "price", "price_cold", "price_warm")
_SAMPLES = 3


def _profile_dbs(data_dir: str) -> list[str]:
    paths = [os.path.join(data_dir, "db.sqlite")] + glob.glob(os.path.join(data_dir, "*", "db.sqlite"))
    # glob skips dot-directories (old .bak-* copies) by default.
    return sorted(p for p in paths if os.path.isfile(p))


def migrate_profile_db(path: str, apply: bool) -> dict[int, int]:
    """Seed ``processed`` and re-key ``exposes``. Returns {old id: new id}."""
    con = sqlite3.connect(path)
    try:
        rows = con.execute(
            "SELECT id, created, details FROM exposes WHERE crawler = ?", (CRAWLER,)
        ).fetchall()
        groups: dict[int, list[tuple[int, str, dict]]] = {}
        mapping: dict[int, int] = {}
        for old_id, created, details in rows:
            expose = json.loads(details)
            url = expose.get("url")
            if not url:
                continue
            new_id = listing_id(url)
            mapping[old_id] = new_id
            groups.setdefault(new_id, []).append((old_id, created, expose))

        processed = {r[0] for r in con.execute("SELECT id FROM processed")}
        to_seed = sorted(set(groups) - processed)
        to_rekey = {new: items for new, items in groups.items()
                    if len(items) > 1 or items[0][0] != new}

        print(f"{path}: {len(rows)} Degewo exposes, {len(groups)} listings; "
              f"{len(to_seed)} new ids to add to processed, "
              f"{len(to_rekey)} listings to re-key in exposes")
        for new in to_seed[:_SAMPLES]:
            olds = ", ".join(str(o) for o, _, _ in groups[new])
            print(f"    {olds} -> {new}  {groups[new][0][2]['url']}")

        if apply and (to_seed or to_rekey):
            with con:
                con.executemany("INSERT INTO processed VALUES (?)", [(i,) for i in to_seed])
                # Delete every affected row first, then insert, so a new id can
                # never collide with an old id deleted later in the loop.
                con.executemany(
                    "DELETE FROM exposes WHERE id = ? AND crawler = ?",
                    [(old, CRAWLER) for items in to_rekey.values() for old, _, _ in items])
                for new, items in to_rekey.items():
                    _, created, expose = max(items, key=lambda item: str(item[1] or ""))
                    con.execute(
                        "INSERT OR REPLACE INTO exposes(id, created, crawler, details) "
                        "VALUES (?, ?, ?, ?)",
                        (new, created, CRAWLER, json.dumps({**expose, "id": new})))
        return mapping
    finally:
        con.close()


def migrate_state_db(path: str, mapping: dict[int, int], apply: bool) -> None:
    """Rewrite auto-apply dedup keys that embed the old id."""
    renames = {old: new for old, new in mapping.items() if old != new}
    con = sqlite3.connect(path)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, column, fmt in (("sends", "listing_key", f"{CRAWLER}:{{}}"),
                                   ("aliases", "scope_key", f"listing:{CRAWLER}:{{}}")):
            if table not in tables:
                continue
            pairs = [(fmt.format(new), fmt.format(old)) for old, new in renames.items()]
            hits = sum(con.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = ?",
                                   (old_key,)).fetchone()[0] for _, old_key in pairs)
            print(f"{path}: {hits} {table} rows to re-key")
            if apply and hits:
                with con:
                    con.executemany(
                        f"UPDATE OR IGNORE {table} SET {column} = ? WHERE {column} = ?", pairs)
    finally:
        con.close()


def migrate_stats_db(path: str, apply: bool) -> None:
    """Re-key Degewo notices, merging rows that are the same listing."""
    cols = ("id", *_STATS_CONTENT, "first_seen_ts", "last_seen_ts")
    con = sqlite3.connect(path)
    try:
        rows = [dict(zip(cols, r)) for r in con.execute(
            f"SELECT {','.join(cols)} FROM notices WHERE crawler = ? ORDER BY first_seen_ts",
            (CRAWLER,))]
        groups: dict[int, list[dict]] = {}
        for row in rows:
            if row["url"]:
                groups.setdefault(listing_id(row["url"]), []).append(row)
        to_rekey = {new: items for new, items in groups.items()
                    if len(items) > 1 or items[0]["id"] != new}
        merged_away = sum(len(items) - 1 for items in to_rekey.values())
        print(f"{path}: {len(rows)} Degewo notices; {len(to_rekey)} listings to re-key, "
              f"{merged_away} duplicate rows to merge")
        if not (apply and to_rekey):
            return
        with con:
            con.executemany(
                "DELETE FROM notices WHERE id = ? AND crawler = ?",
                [(row["id"], CRAWLER) for items in to_rekey.values() for row in items])
            for new, items in to_rekey.items():
                merged = {col: next((r[col] for r in items if (r[col] or "").strip()), "")
                          for col in _STATS_CONTENT}
                first_seen = min(r["first_seen_ts"] for r in items)
                last_seen = max((r["last_seen_ts"] or r["first_seen_ts"]) for r in items)
                con.execute(
                    f"INSERT OR REPLACE INTO notices (id, crawler, {','.join(_STATS_CONTENT)}, "
                    f"first_seen_ts, last_seen_ts) VALUES ({','.join('?' * (len(_STATS_CONTENT) + 4))})",
                    (new, CRAWLER, *(merged[c] for c in _STATS_CONTENT), first_seen, last_seen))
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Re-key Degewo listings to URL-derived ids")
    ap.add_argument("--data-dir", default="/data", help="state directory (default: /data)")
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.data_dir):
        print(f"no such directory: {args.data_dir}", file=sys.stderr)
        return 2
    for db in _profile_dbs(args.data_dir):
        mapping = migrate_profile_db(db, args.apply)
        state_db = os.path.join(os.path.dirname(db), "state.db")
        if mapping and os.path.isfile(state_db):
            migrate_state_db(state_db, mapping, args.apply)
    stats_db = os.path.join(args.data_dir, "stats.db")
    if os.path.isfile(stats_db):
        migrate_stats_db(stats_db, args.apply)
    print("applied." if args.apply else "DRY RUN — nothing written; re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
