"""Tests for StatsLogger and StatsProcessor"""
import os
import tempfile
import time
import unittest

from berlin_flat_hunter.stats import StatsLogger, StatsProcessor

EXPOSE_A = {
    "id": 1001, "url": "https://www.gewobag.de/1/", "title": "Wohnung A",
    "address": "Straße 1, 10115 Berlin", "rooms": "2", "size": "60 m²", "price": "900 €",
    "crawler": "Gewobag",
}
EXPOSE_B = {
    "id": 2002, "url": "https://www.wbm.de/1/", "title": "Wohnung B",
    "address": "Straße 2, 10178 Berlin", "rooms": "3", "size": "72 m²", "price": "1100 €",
    "crawler": "Wbm",
}


def _tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return path


class TestStatsLogger(unittest.TestCase):

    def setUp(self):
        self.path = _tmp_db()
        self.stats = StatsLogger(self.path)

    def tearDown(self):
        self.stats.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_logs_new_expose(self):
        self.assertTrue(self.stats.log(EXPOSE_A))
        self.assertEqual(self.stats.count_total(), 1)

    def test_duplicate_ignored(self):
        self.stats.log(EXPOSE_A)
        self.assertFalse(self.stats.log(EXPOSE_A))
        self.assertEqual(self.stats.count_total(), 1)

    def test_count_by_crawler(self):
        self.stats.log(EXPOSE_A)
        self.stats.log(EXPOSE_B)
        counts = self.stats.count_by_crawler()
        self.assertEqual(counts["Gewobag"], 1)
        self.assertEqual(counts["Wbm"], 1)

    def test_count_since(self):
        before = time.time()
        self.stats.log(EXPOSE_A)
        self.assertEqual(self.stats.count_since(before), 1)
        self.assertEqual(self.stats.count_since(time.time() + 10), 0)

    def test_skips_expose_without_id(self):
        self.assertFalse(self.stats.log({"url": "https://example.com/"}))
        self.assertEqual(self.stats.count_total(), 0)

    def test_recent_returns_newest_first(self):
        self.stats.log(EXPOSE_A)
        time.sleep(0.01)
        self.stats.log(EXPOSE_B)
        recent = self.stats.recent(limit=2)
        self.assertEqual(recent[0]["id"], EXPOSE_B["id"])
        self.assertEqual(recent[1]["id"], EXPOSE_A["id"])

    def test_recent_respects_limit(self):
        self.stats.log(EXPOSE_A)
        self.stats.log(EXPOSE_B)
        self.assertEqual(len(self.stats.recent(limit=1)), 1)

    def test_persistence_across_instances(self):
        self.stats.log(EXPOSE_A)
        self.stats.close()
        stats2 = StatsLogger(self.path)
        self.assertEqual(stats2.count_total(), 1)
        stats2.close()

    def test_preserves_all_fields(self):
        self.stats.log(EXPOSE_A)
        row = self.stats.recent(limit=1)[0]
        self.assertEqual(row["title"], EXPOSE_A["title"])
        self.assertEqual(row["address"], EXPOSE_A["address"])
        self.assertEqual(row["price"], EXPOSE_A["price"])
        self.assertEqual(row["crawler"], EXPOSE_A["crawler"])

    def test_same_id_different_crawlers_both_kept(self):
        """Different crawlers may mint the same numeric ID — both must be stored."""
        a = {**EXPOSE_A, "id": 42, "crawler": "Gewobag", "title": "From Gewobag"}
        b = {**EXPOSE_A, "id": 42, "crawler": "Wbm", "title": "From Wbm"}
        self.assertTrue(self.stats.log(a))
        self.assertTrue(self.stats.log(b))
        self.assertEqual(self.stats.count_total(), 2)
        counts = self.stats.count_by_crawler()
        self.assertEqual(counts["Gewobag"], 1)
        self.assertEqual(counts["Wbm"], 1)

    def test_same_id_same_crawler_dedups(self):
        """Re-logging the same (id, crawler) pair must dedup (composite PK)."""
        a = {**EXPOSE_A, "id": 42, "crawler": "Gewobag"}
        self.assertTrue(self.stats.log(a))
        self.assertFalse(self.stats.log(a))
        self.assertEqual(self.stats.count_total(), 1)

    def test_stores_cold_warm_price(self):
        expose = {**EXPOSE_A, "price_cold": "900 €", "price_warm": "1100 €"}
        self.stats.log(expose)
        row = self.stats.recent(limit=1)[0]
        self.assertEqual(row["price_cold"], "900 €")
        self.assertEqual(row["price_warm"], "1100 €")

    def test_backfill_fills_empty_fields_on_resight(self):
        """Teaser first, concrete data later — empty fields must fill in."""
        teaser = {"id": 7, "crawler": "Gewobag", "url": "https://g/7/",
                  "title": "Neubau", "address": "", "rooms": "", "size": "",
                  "price": "Auf Anfrage", "price_cold": "", "price_warm": ""}
        self.assertTrue(self.stats.log(teaser))
        published = {**teaser, "address": "Weg 1, 12557 Berlin", "rooms": "4",
                     "size": "109,92 m²", "price_cold": "1.483,92 Euro",
                     "price_warm": "1.901,62 Euro"}
        self.assertFalse(self.stats.log(published))  # re-sight, not new
        self.assertEqual(self.stats.count_total(), 1)
        row = self.stats.recent(limit=1)[0]
        self.assertEqual(row["address"], "Weg 1, 12557 Berlin")
        self.assertEqual(row["rooms"], "4")
        self.assertEqual(row["size"], "109,92 m²")
        self.assertEqual(row["price_cold"], "1.483,92 Euro")

    def test_backfill_does_not_overwrite_populated_fields(self):
        self.stats.log({**EXPOSE_A, "id": 8})
        self.stats.log({**EXPOSE_A, "id": 8, "rooms": "99", "price": "1 €"})
        row = self.stats.recent(limit=1)[0]
        self.assertEqual(row["rooms"], EXPOSE_A["rooms"])  # unchanged
        self.assertEqual(row["price"], EXPOSE_A["price"])

    def test_last_seen_ts_bumped_on_resight(self):
        self.stats.log({**EXPOSE_A, "id": 9})
        first = self.stats.recent(limit=1)[0]["last_seen_ts"]
        time.sleep(0.01)
        self.stats.log({**EXPOSE_A, "id": 9})
        second = self.stats.recent(limit=1)[0]["last_seen_ts"]
        self.assertGreater(second, first)
        # first_seen_ts must be preserved across the re-sight
        self.assertEqual(self.stats.recent(limit=1)[0]["first_seen_ts"],
                         first)

    def test_migrates_legacy_schema_without_new_columns(self):
        """A pre-existing DB lacking price_cold/price_warm/last_seen_ts opens clean."""
        import sqlite3
        legacy = _tmp_db()
        con = sqlite3.connect(legacy)
        con.executescript(
            "CREATE TABLE notices (id INTEGER NOT NULL, crawler TEXT NOT NULL, "
            "url TEXT, title TEXT, address TEXT, rooms TEXT, size TEXT, price TEXT, "
            "first_seen_ts REAL NOT NULL, PRIMARY KEY (id, crawler));"
        )
        con.execute("INSERT INTO notices VALUES (5,'Gewobag','u','t','a','1','50 m²','9 €',1.0)")
        con.commit(); con.close()
        migrated = StatsLogger(legacy)
        try:
            self.assertEqual(migrated.count_total(), 1)
            # new column readable (NULL for the legacy row), new writes work
            self.assertIsNone(migrated.recent(limit=1)[0]["price_cold"])
            self.assertTrue(migrated.log({**EXPOSE_A, "id": 6, "price_cold": "7 €"}))
        finally:
            migrated.close()
            os.unlink(legacy)


class TestStatsProcessor(unittest.TestCase):

    def setUp(self):
        self.path = _tmp_db()
        self.stats = StatsLogger(self.path)
        self.proc = StatsProcessor(self.stats)

    def tearDown(self):
        self.stats.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_process_returns_expose_unchanged(self):
        result = self.proc.process_expose(dict(EXPOSE_A))
        self.assertEqual(result["id"], EXPOSE_A["id"])

    def test_process_logs_to_db(self):
        self.proc.process_expose(dict(EXPOSE_A))
        self.assertEqual(self.stats.count_total(), 1)

    def test_process_is_non_destructive_on_log_error(self):
        broken_stats = StatsLogger(self.path)
        broken_stats.close()  # connection closed → log will raise
        proc = StatsProcessor(broken_stats)
        # Should not raise — processor swallows and logs
        result = proc.process_expose(dict(EXPOSE_A))
        self.assertEqual(result["id"], EXPOSE_A["id"])


if __name__ == "__main__":
    unittest.main()
