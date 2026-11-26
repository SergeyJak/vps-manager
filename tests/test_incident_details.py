import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from iptv_monitoring import incidents

CHANNEL = "p2:0123456789abcdef0123"
OCCUPIED = "p2:456789abcdef01234567"
NOW = 2_000_000_000


class IncidentDetailsTest(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.project = Path(root.name)
        self.path = self.project / ".iptv_state" / "telemetry.sqlite3"
        self.path.parent.mkdir()

    def create_db(self, diagnostics=True):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TABLE incidents (
                id INTEGER PRIMARY KEY, occurred_at INTEGER, channel_id TEXT,
                kind TEXT, detail TEXT, duration_seconds REAL)""")
            if diagnostics:
                db.execute("ALTER TABLE incidents ADD COLUMN diagnostics TEXT")

    def test_filters_stream_rejections_and_reports_occupancy_without_viewer_data(self):
        self.create_db()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executemany("INSERT INTO incidents VALUES (NULL, ?, ?, ?, ?, NULL, ?)", [
                (NOW - 91, CHANNEL, "client_error", "HTTP_503_STREAM_LIMIT", None),
                (NOW - 59, CHANNEL, "client_error", "HTTP_503_STREAM_LIMIT", "invalid-json"),
                (NOW - 40, OCCUPIED, "client_error", "HTTP_503_STREAM_LIMIT", None),
                (NOW - 39, CHANNEL, "cdn_decision", "START", None),
                (NOW - 30, CHANNEL, "client_error", "HTTP_503_STREAM_LIMIT", json.dumps({
                    "max_streams": 1, "route": "manifest", "client": "private-viewer",
                    "active": [{"channel_id": OCCUPIED, "pinned": False, "standby": False,
                                "pending_requests": 1, "idle_seconds": 2.5,
                                "url": "https://private.example/secret"}],
                    "token": "private-token",
                })),
                (NOW - 20, CHANNEL, "client_error", "HTTP_503_STREAM_LIMIT", None),
            ])

        args = dict(start_at=NOW - 90, end_at=NOW - 30, kind="client_error",
                    channel_id=CHANNEL, now=NOW)
        report = incidents(self.project, limit=1, **args)
        self.assertEqual(report["state"], "ready")
        self.assertEqual(report["incident_count"], 2)
        self.assertTrue(report["truncated"])
        self.assertEqual(report["incidents"], [{
            "occurred_at": NOW - 30, "channel_id": CHANNEL, "kind": "client_error",
            "reason": "HTTP_503_STREAM_LIMIT", "duration_seconds": None,
            "diagnostics": {"max_streams": 1, "route": "manifest", "active": [{
                "channel_id": OCCUPIED, "pinned": False, "standby": False,
                "pending_requests": 1, "idle_seconds": 2.5,
            }]},
        }])
        all_rows = incidents(self.project, **args)
        self.assertFalse(all_rows["truncated"])
        self.assertIsNone(all_rows["incidents"][1]["diagnostics"])

    def test_preserves_cdn_and_source_details_from_their_known_fields(self):
        self.create_db()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executemany("INSERT INTO incidents VALUES (NULL, ?, ?, ?, ?, ?, ?)", [
                (NOW - 10, CHANNEL, "upstream_error", "MANIFEST_HTTP_404", 3.2,
                 json.dumps({"buffer_state": "READY", "contiguous_tail_seconds": 18,
                             "last_downloaded_sequence": 33, "private": "secret"})),
                (NOW - 10, CHANNEL, "cdn_decision", "NO_READY_BACKUP", None,
                 json.dumps({"selected_slot": 2, "from_slot": 1, "sources": {
                     "1": {"state": "STREAM_LIMIT", "max_streams": 1, "active": [{
                         "channel_id": "https://private.example/secret", "pending_requests": 1,
                     }]},
                     "2": {"ready": True, "fresh": False, "age_seconds": 25},
                     "3": {"state": "private-token"},
                 }})),
            ])
        cdn, upstream = incidents(self.project, now=NOW)["incidents"]
        self.assertEqual(cdn["reason"], "NO_READY_BACKUP")
        self.assertEqual(cdn["diagnostics"], {"selected_slot": 2, "from_slot": 1, "sources": {
            "1": {"state": "STREAM_LIMIT", "max_streams": 1,
                  "active": [{"pending_requests": 1}]},
            "2": {"ready": True, "fresh": False, "age_seconds": 25},
        }})
        self.assertEqual(upstream["reason"], "MANIFEST_HTTP_404")
        self.assertEqual(upstream["diagnostics"], {"buffer_state": "READY",
                         "contiguous_tail_seconds": 18, "last_downloaded_sequence": 33})

    def test_reads_a_legacy_journal_and_redacts_free_form_details(self):
        self.create_db(diagnostics=False)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO incidents VALUES (NULL, ?, ?, ?, ?, NULL)",
                       (NOW - 10, CHANNEL, "upstream_error", "https://private.example/secret"))
        report = incidents(self.project, now=NOW)
        self.assertEqual(report["state"], "ready")
        self.assertEqual(report["incident_count"], 1)
        self.assertIsNone(report["incidents"][0]["reason"])
        self.assertIsNone(report["incidents"][0]["diagnostics"])

    def test_missing_database_is_distinct_from_an_empty_journal(self):
        self.assertEqual(incidents(self.project, now=NOW)["state"], "missing")
        self.assertFalse(self.path.exists())
        self.create_db()
        report = incidents(self.project, now=NOW)
        self.assertEqual(report["state"], "ready")
        self.assertEqual(report["incident_count"], 0)

    def test_ignores_non_numeric_metrics_in_the_journal(self):
        self.create_db()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO incidents VALUES (NULL, ?, ?, ?, ?, ?, ?)",
                       (NOW - 10, CHANNEL, "segment_slow", "SEGMENT_DOWNLOAD_SLOW", "private-text",
                        '{"startup_wait_seconds": 1e100, "max_streams": false}'))
        event = incidents(self.project, now=NOW)["incidents"][0]
        self.assertEqual(event["reason"], "SEGMENT_DOWNLOAD_SLOW")
        self.assertIsNone(event["duration_seconds"])
        self.assertIsNone(event["diagnostics"])

    def test_rejects_invalid_filters(self):
        for args in ({"start_at": NOW}, {"start_at": NOW - 31 * 86400}, {"start_at": False},
                     {"end_at": NOW + 1}, {"limit": 0}, {"limit": 201},
                     {"kind": "unsupported"}, {"channel_id": "../../file"}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                incidents(self.project, now=NOW, **args)
