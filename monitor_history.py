"""Persist bounded resource samples independently of the IPTV processes."""
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from iptv_monitoring import gateway_snapshot, read_only, window

RETENTION_SECONDS = 30 * 86400
FIELDS = (
    "sampled_at", "revision", "verified_revision", "gateway_pid", "gateway_memory_bytes",
    "gateway_cpu_ns", "admin_memory_bytes", "admin_cpu_ns", "memory_available_bytes",
    "disk_free_bytes", "load_1m", "network_rx_bytes", "network_tx_bytes", "active_streams",
)


def save_sample(path, sample):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Invalid history file")
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(path, timeout=5)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS samples (
            sampled_at INTEGER PRIMARY KEY, revision TEXT, verified_revision TEXT, gateway_pid INTEGER,
            gateway_memory_bytes INTEGER, gateway_cpu_ns INTEGER, admin_memory_bytes INTEGER,
            admin_cpu_ns INTEGER, memory_available_bytes INTEGER, disk_free_bytes INTEGER,
            load_1m REAL, network_rx_bytes INTEGER, network_tx_bytes INTEGER, active_streams INTEGER
        )""")
        columns = ", ".join(FIELDS)
        marks = ", ".join("?" for _ in FIELDS)
        db.execute(f"INSERT OR REPLACE INTO samples ({columns}) VALUES ({marks})",
                   [sample.get(field) for field in FIELDS])
        db.execute("DELETE FROM samples WHERE sampled_at < (SELECT MAX(sampled_at) FROM samples) - ?",
                   (RETENTION_SECONDS,))


def read_history(path, days=1, limit=288, end_at=None, *, now=None):
    start, end = window(days, end_at, now=now)
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("limit must be an integer from 1 to 500")
    report = {"state": "missing", "start_at": start, "end_at": end, "sample_count": 0,
              "truncated": False, "samples": [], "revisions": []}
    try:
        with closing(read_only(path)) as db, db:
            db.execute("BEGIN")
            args = start, end
            count, first, last = db.execute(
                "SELECT COUNT(*), MIN(sampled_at), MAX(sampled_at) FROM samples WHERE sampled_at BETWEEN ? AND ?",
                args).fetchone()
            rows = db.execute("""SELECT * FROM samples WHERE sampled_at BETWEEN ? AND ?
                               ORDER BY sampled_at DESC LIMIT ?""", (*args, limit)).fetchall()
            revisions = db.execute("""WITH ordered AS (
                SELECT sampled_at, revision, verified_revision,
                       LAG(revision) OVER (ORDER BY sampled_at) AS previous,
                       LAG(verified_revision) OVER (ORDER BY sampled_at) AS previous_verified
                FROM samples WHERE sampled_at BETWEEN ? AND ?
            ) SELECT sampled_at, revision, verified_revision FROM ordered
              WHERE revision IS NOT previous OR verified_revision IS NOT previous_verified
              ORDER BY sampled_at DESC LIMIT 100""", args).fetchall()
            peaks = db.execute("""SELECT MAX(gateway_memory_bytes) AS gateway_memory_bytes,
                                      MIN(memory_available_bytes) AS memory_available_bytes,
                                      MAX(load_1m) AS load_1m, MAX(active_streams) AS active_streams
                               FROM samples WHERE sampled_at BETWEEN ? AND ?""", args).fetchone()
    except sqlite3.Error:
        if path.exists():
            report["state"] = "unreadable"
        return report
    report.update(state="ready", sample_count=count, truncated=count > limit,
                  first_sample_at=first, last_sample_at=last, window_extremes=dict(peaks),
                  samples=[dict(row) for row in reversed(rows)],
                  revisions=[dict(row) for row in reversed(revisions)])
    return report


def _number(value):
    return int(value) if isinstance(value, str) and value.isdecimal() and len(value) <= 19 and int(value) < 2**63 else None


def _network():
    try:
        lines = Path("/proc/net/dev").read_text().splitlines()[2:]
        counters = [values.split() for line in lines
                    for name, _, values in [line.partition(":")] if name.strip() != "lo"]
        return sum(int(row[0]) for row in counters), sum(int(row[8]) for row in counters)
    except (OSError, ValueError, IndexError):
        return None, None


def collect_sample(manager):
    sampled_at = int(time.time())
    host = manager.host_metrics()
    gateway = manager.service_status("iptv-gateway.service").get("properties", {})
    admin = manager.service_status("iptv-admin.service").get("properties", {})
    rx, tx = _network()
    verified = manager._verified_revision(manager.WRITE_FLAG.with_name("verified-revision"))
    return {
        "sampled_at": sampled_at, "revision": manager.revision(),
        "verified_revision": verified if verified and len(verified) == 40 and
        all(c in "0123456789abcdef" for c in verified) else None,
        "gateway_pid": _number(gateway.get("MainPID")) or None,
        "gateway_memory_bytes": _number(gateway.get("MemoryCurrent")),
        "gateway_cpu_ns": _number(gateway.get("CPUUsageNSec")),
        "admin_memory_bytes": _number(admin.get("MemoryCurrent")),
        "admin_cpu_ns": _number(admin.get("CPUUsageNSec")),
        "memory_available_bytes": host["memory_available_bytes"], "disk_free_bytes": host["disk_free_bytes"],
        "load_1m": host["load_average"][0], "network_rx_bytes": rx, "network_tx_bytes": tx,
        "active_streams": gateway_snapshot(manager.PROJECT)["active_streams"],
    }


if __name__ == "__main__":
    import manager

    save_sample(manager.WRITE_FLAG.with_name("monitor-history.sqlite3"), collect_sample(manager))
