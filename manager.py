"""Read-only VPS diagnostics; no arbitrary commands or raw journal data."""
import json
import os
import shutil
import subprocess
from pathlib import Path

UNITS = frozenset(("iptv-gateway.service", "iptv-admin.service"))
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "SYSTEMD_PAGER": "cat"}

def run_fixed(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True,
                                timeout=8, env=ENV, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result if result.returncode == 0 else None

def validate_unit(unit):
    if unit not in UNITS:
        raise ValueError("Unknown service")

def host_metrics():
    fields = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, value = line.partition(":")
        if key in ("MemTotal", "MemAvailable"):
            fields[key] = int(value.split()[0]) * 1024
    disk = shutil.disk_usage("/")
    return {"cpu_count": os.cpu_count(), "load_average": os.getloadavg(),
            "memory_total_bytes": fields["MemTotal"],
            "memory_available_bytes": fields["MemAvailable"],
            "disk_total_bytes": disk.total, "disk_free_bytes": disk.free,
            "uptime_seconds": float(Path("/proc/uptime").read_text().split()[0])}

def service_status(unit):
    validate_unit(unit)
    properties = ("LoadState", "ActiveState", "SubState", "MainPID", "NRestarts",
                  "MemoryCurrent", "CPUUsageNSec")
    result = run_fixed(["/usr/bin/systemctl", "show", "--no-pager",
                        "--property=" + ",".join(properties), unit])
    if result is None:
        return {"available": False, "service": unit}
    values = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key in properties:
            values[key] = value
    return {"available": True, "service": unit, "properties": values}

def journal_summary(unit, minutes=60):
    validate_unit(unit)
    if type(minutes) is not int or not 1 <= minutes <= 1440:
        raise ValueError("minutes must be an integer from 1 to 1440")
    result = run_fixed(["/usr/bin/journalctl", "--no-pager", "--quiet",
                        "--unit=" + unit, "--since=-%dmin" % minutes,
                        "--lines=500", "--output=json",
                        "--output-fields=PRIORITY,__REALTIME_TIMESTAMP"])
    if result is None:
        return {"available": False, "service": unit}
    # No journal payload or stderr crosses the tool boundary.
    counts = {}
    last = None
    for line in result.stdout.splitlines():
        try:
            item = json.loads(line)
            priority = int(item.get("PRIORITY", 6))
            ts = int(item["__REALTIME_TIMESTAMP"]) / 1_000_000
        except (ValueError, TypeError, KeyError):
            continue
        if not 0 <= priority <= 7:
            continue
        counts[str(priority)] = counts.get(str(priority), 0) + 1
        last = ts if last is None else max(last, ts)
    total = sum(counts.values())
    return {"available": True, "service": unit, "minutes": minutes,
            "sample_count": total, "possibly_truncated": total >= 500,
            "priority_counts": counts, "last_timestamp": last,
            "note": "Empty results may mean no entries or insufficient journal permissions."}
