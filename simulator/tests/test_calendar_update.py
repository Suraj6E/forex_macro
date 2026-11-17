"""Keeping the calendar current: when the updater runs fxmacro's refresh,
what it reports, and that the calendar sees a write that is still in the
database's write-ahead log."""

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

from simulator.data import calendar_update as cu
from simulator.data.calendar import Calendar
from simulator.tests.test_markets import SCHEMA, ns

HOUR = 3600


class Schedule(unittest.TestCase):
    def test_every_three_hours_or_three_minutes_after_a_release(self):
        last = 1_000_000.0
        self.assertEqual(cu.next_run_s(last, None), last + 3 * HOUR)
        self.assertEqual(cu.next_run_s(last, last + HOUR), last + HOUR + 180)
        self.assertEqual(cu.next_run_s(last, last + 5 * HOUR), last + 3 * HOUR)

    def test_never_within_ten_minutes_of_the_last_run(self):
        last = 1_000_000.0
        self.assertEqual(cu.next_run_s(last, last - 60), last + 600)

    def test_a_failure_waits_half_an_hour_whatever_is_released(self):
        last = 1_000_000.0
        self.assertEqual(cu.next_run_s(last, last + 60, failed=True), last + 1800)


def make_db(path: Path, wal: bool = False):
    db = sqlite3.connect(path)
    if wal:
        db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    db.executemany("INSERT INTO calendar_data_indicator VALUES (?,?,?,?,?,?,?,?,?,?)", [
        (1, "USD", 3, "Non-Farm Employment Change", "labour", "", "", "", "", "auto"),
        (2, "USD", 1, "Leading Index", "", "", "", "", "", "auto"),
    ])
    db.executemany("INSERT INTO calendar_data_eventrelease VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        (10, 1, None, "a", "2026-11-06 13:30:00", None, None, None, None, None, None, None, "minute", "", 0, "{}"),
        (11, 2, None, "b", "2026-11-09 15:00:00", None, None, None, None, None, None, None, "minute", "", 0, "{}"),
        (12, 1, None, "c", "2026-12-04 13:30:00", None, None, None, None, None, None, None, "minute", "", 0, "{}"),
    ])
    db.commit()
    return db


class CalendarFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "db.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_next_release_at_or_above_an_impact(self):
        make_db(self.path).close()
        cal = Calendar(self.path)
        self.assertEqual(cal.next_after(ns(2026, 11, 6, 14), 1), ns(2026, 11, 9, 15))
        self.assertEqual(cal.next_after(ns(2026, 11, 6, 14), 2), ns(2026, 12, 4, 13, 30))
        self.assertIsNone(cal.next_after(ns(2026, 12, 5), 1))

    def test_a_write_still_in_the_write_ahead_log_is_seen(self):
        writer = make_db(self.path, wal=True)
        writer.execute("PRAGMA wal_autocheckpoint=0")  # keep the write in the log, as a busy fxmacro does
        cal = Calendar(self.path)
        self.assertEqual(cal.status()["events"], 3)
        writer.execute("INSERT INTO calendar_data_eventrelease VALUES (13,1,NULL,'d','2026-12-11 13:30:00',NULL,"
                       "NULL,NULL,NULL,NULL,NULL,NULL,'minute','',0,'{}')")
        writer.commit()
        cal._checked = 0.0  # as if the 30 s had passed
        self.assertEqual(cal.status()["events"], 4)
        writer.close()


class Updater(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "db.sqlite3"
        make_db(self.path).close()
        self.cal = Calendar(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def updater(self, code: str):
        return cu.CalendarUpdater(self.cal, command=[sys.executable, "-c", code], enabled=True)

    def test_a_run_reports_what_was_fetched_and_rereads_the_calendar(self):
        out = {"weekly": "weekly feed: next allowed in 20.0 h", "pages_mode": "recent",
               "pages": "calendar pages (recent, Nov 2026): 120 seen, 3 new, 0 with a cross-source disagreement, "
                        "0 newly unmapped; 1 older snapshot(s) of the same pages replaced (2 MB)", "errors": [],
               "twins": "0 duplicate indicator(s) folded"}
        up = self.updater(f"print('log line'); print({json.dumps(json.dumps(out))})")
        self.cal.status()
        self.assertTrue(up.run_once())
        self.assertEqual(up.status["state"], "ok")
        self.assertEqual(up.status["message"], "calendar pages (recent, Nov 2026): 120 seen, 3 new")
        self.assertIsNone(self.cal._version)  # read again at the next look

    def test_nothing_due_says_so(self):
        out = {"weekly": "weekly feed: next allowed in 20.0 h", "pages_mode": "skip",
               "pages": "calendar pages: fetched 4 min ago", "errors": [], "twins": ""}
        up = self.updater(f"print({json.dumps(json.dumps(out))})")
        self.assertTrue(up.run_once())
        self.assertTrue(up.status["message"].startswith("Nothing was due"))

    def test_a_failure_shows_its_reason(self):
        out = {"weekly": "", "pages_mode": "full", "pages": "", "errors": ["calendar pages (full) failed: HTTP 403"]}
        up = self.updater(f"import sys; print({json.dumps(json.dumps(out))}); sys.exit(1)")
        self.assertFalse(up.run_once())
        self.assertEqual(up.status["state"], "error")
        self.assertIn("HTTP 403", up.status["message"])

    def test_update_now_runs_straight_away_in_the_background(self):
        out = {"weekly": "", "pages_mode": "recent", "pages": "calendar pages (recent, Nov 2026): ok", "errors": []}
        up = self.updater(f"import sys; print({json.dumps(json.dumps(out))}); print(sys.argv[1:])")
        up.start(first_delay_s=3600)
        try:
            self.assertTrue(up.run_now())
            for _ in range(100):
                if up.status["state"] == "ok":
                    break
                time.sleep(0.1)
            self.assertEqual(up.status["state"], "ok")
            self.assertGreater(up.status["next_ms"], time.time() * 1000)
        finally:
            up.stop()

    def test_off_when_another_database_is_read(self):
        up = cu.CalendarUpdater(self.cal)
        self.assertFalse(up.enabled)
        self.assertEqual(up.status["state"], "off")
        self.assertFalse(up.run_now())


if __name__ == "__main__":
    unittest.main()
