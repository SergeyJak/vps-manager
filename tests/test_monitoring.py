import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import iptv_monitoring as monitoring
import monitor_history as history
import playback_probe as playback

CHANNEL = "0123456789abcdef0123"
NOW = 2_000_000_000


class StabilityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.db = self.project / ".iptv_state" / "telemetry.sqlite3"
        self.db.parent.mkdir()
        self.patch = patch.object(monitoring, "gateway_snapshot", return_value={
            "available": True, "telemetry_enabled": True, "telemetry_dropped": 0,
            "active_streams": 1,
        })
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_missing_database_is_not_an_empty_report(self):
        report = monitoring.stability(self.project, 1, now=NOW)
        self.assertEqual(report["coverage"]["database"], "missing")
        self.assertIsNone(report["totals"])
        self.assertFalse(self.db.exists())

    def test_window_metrics_and_safe_projection(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executescript("""
                CREATE TABLE incidents (id INTEGER PRIMARY KEY, occurred_at INT, channel_id TEXT,
                                        kind TEXT, detail TEXT, duration_seconds REAL, diagnostics TEXT);
                CREATE TABLE viewing_sessions (started_at INT, last_seen INT, estimated_seconds INT, segment_requests INT);
                CREATE TABLE delivery_monitor (id INT, installed INT);
                CREATE TABLE delivery_reset (id INT, reset_at INT);
                CREATE TABLE delivery_episodes (id INT, channel_id TEXT, viewer TEXT, reason TEXT,
                                                started INT, confirmed INT, recovered INT, recovery_slot INT);
            """)
            conn.execute("INSERT INTO delivery_monitor VALUES (1, ?)", (NOW - 50_000,))
            conn.execute("INSERT INTO delivery_reset VALUES (1, ?)", (NOW - 600,))
            conn.executemany("INSERT INTO incidents VALUES (NULL, ?, ?, ?, ?, ?, ?)", [
                (NOW - 50, CHANNEL, "client_error", "HTTP_404_SEGMENT_CACHE_MISS", None, "PRIVATE_DATA"),
                (NOW - 40, CHANNEL, "stream_recovered", "SECRET_TOKEN", 8.5, "PRIVATE_DATA"),
                (NOW - 90_000, CHANNEL, "segment_slow", "OLD", 11, None),
                (NOW + 1, CHANNEL, "segment_slow", "FUTURE", 12, None),
            ])
            conn.execute("INSERT INTO viewing_sessions VALUES (?, ?, 3600, 9)", (NOW - 3700, NOW - 80))
            conn.execute("INSERT INTO delivery_episodes VALUES (1, ?, 'PRIVATE_VIEWER', 'playlist', ?, ?, ?, 2)",
                         (CHANNEL, NOW - 120, NOW - 115, NOW - 50))
        report = monitoring.stability(self.project, 1, now=NOW)
        self.assertEqual(report["totals"]["incidents"], 2)
        self.assertEqual(report["totals"]["client_segment_failure_fraction"], 0.1)
        self.assertEqual(report["totals"]["delivery_episodes"], 1)
        self.assertEqual(report["totals"]["median_recovery_seconds"], 70)
        self.assertEqual(report["recent_episodes"][0]["duration_seconds"], 70)
        self.assertEqual(report["coverage"]["delivery_episodes_since"], NOW - 599)
        serialized = json.dumps(report)
        for value in ("PRIVATE_DATA", "PRIVATE_VIEWER", "SECRET_TOKEN"):
            self.assertNotIn(value, serialized)


class HistoryTest(unittest.TestCase):
    def test_samples_are_filtered_and_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "history.sqlite3"
            self.assertEqual(history.read_history(path, 1, 2, now=NOW)["state"], "missing")
            for offset in (30 * 86400 + 200, 500, 300, 200, 100):
                history.save_sample(path, {
                    "sampled_at": NOW - offset, "revision": ("a" if offset > 250 else "b") * 40,
                    "gateway_memory_bytes": 1234,
                })
            result = history.read_history(path, 1, 2, now=NOW)
            self.assertEqual(result["sample_count"], 4)
            self.assertTrue(result["truncated"])
            self.assertEqual([x["sampled_at"] for x in result["samples"]], [NOW - 200, NOW - 100])
            self.assertEqual([x["revision"] for x in result["revisions"]], ["a" * 40, "b" * 40])
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 4)


class ProbeTest(unittest.TestCase):
    def test_requires_local_opt_in(self):
        with tempfile.TemporaryDirectory() as root:
            result = playback.probe(Path(root), "https://tv.example")
            self.assertEqual(result, {"ok": None, "state": "not_configured"})

    def test_follows_a_new_segment_on_the_same_route(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root)
            (config / "playback-probe.json").write_text(json.dumps(
                {"token": "a" * 32, "channel_id": CHANNEL}), encoding="utf-8")
            prefix = "https://tv.example/" + "a" * 32 + "/channel/" + CHANNEL + "/"
            def manifest(number):
                return f"#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\nsegment/{number}.ts\n".encode()
            with patch.object(playback, "_download", side_effect=[manifest(1), manifest(2), b"media"]) as download:
                result = playback.probe(config, "https://tv.example", wait_seconds=1, interval=0)
            self.assertTrue(result["ok"])
            self.assertEqual(download.call_args.args[0], prefix + "segment/2.ts")
            self.assertNotIn("a" * 32, json.dumps(result))

    def test_http_probe_reads_one_range_from_the_advancing_playlist(self):
        requested = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                requested.append((self.path, self.headers.get("Range")))
                if self.path.endswith("/index.m3u8"):
                    status = 200
                    body = (f"#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\n"
                            f"segment/{len(requested)}.ts\n").encode()
                else:
                    status, body = 206, b"media"
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        token = "b" * 32
        with HTTPServer(("127.0.0.1", 0), Handler) as server, tempfile.TemporaryDirectory() as root:
            background = threading.Thread(target=server.serve_forever, daemon=True)
            background.start()
            try:
                config = Path(root)
                (config / "playback-probe.json").write_text(
                    json.dumps({"token": token, "channel_id": CHANNEL}), encoding="utf-8")
                result = playback.probe(config, f"http://127.0.0.1:{server.server_port}", wait_seconds=2, interval=0)
            finally:
                server.shutdown()
                background.join()
        self.assertTrue(result["ok"])
        self.assertEqual(result["segment_bytes"], 5)
        self.assertEqual(requested[-1], (f"/{token}/channel/{CHANNEL}/segment/2.ts", "bytes=0-65535"))

    def test_rejects_segments_outside_the_configured_route(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root)
            (config / "playback-probe.json").write_text(json.dumps(
                {"token": "a" * 32, "channel_id": CHANNEL}), encoding="utf-8")
            with patch.object(playback, "_download", return_value=(
                b"#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\nhttps://foreign.example/segment/1.ts\n")) as download:
                result = playback.probe(config, "https://tv.example")
            self.assertFalse(result["ok"])
            self.assertEqual(result["state"], "invalid_manifest")
            self.assertEqual(download.call_count, 1)
