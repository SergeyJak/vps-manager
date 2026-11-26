"""VPS diagnostics and explicitly enabled, fixed TV operations."""
import json
import os
import shutil
import subprocess
import fcntl
import urllib.request
import urllib.error
from contextlib import contextmanager
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

# Never accept an arbitrary path, branch, URL, command, or environment from MCP.
PROJECT = Path.home() / "Tvtest"
REMOTE = "https://github.com/SergeyJak/Tvtest"
HEALTH_URL = "https://tv.heysmart.lv/admin/"
WRITE_FLAG = Path.home() / ".config/vps-manager/enable-writes"
CONFIRM = "CONFIRM_TV_CHANGE"

def require_write(confirm):
    if confirm != CONFIRM:
        raise ValueError("Explicit confirmation required")
    if os.geteuid() == 0:
        raise PermissionError("Do not run as root")
    if not WRITE_FLAG.is_file():
        raise PermissionError("Write operations disabled on VPS")

@contextmanager
def operation_lock():
    directory = Path.home() / ".config/vps-manager"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / "operation.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another VPS operation is running") from None
        yield

def execute(args, cwd=None, timeout=60):
    # Do not return stdout/stderr to MCP: tests and Git may contain secrets.
    env = dict(ENV, HOME=str(Path.home()), GIT_TERMINAL_PROMPT="0",
               GIT_CONFIG_NOSYSTEM="1", PYTHONDONTWRITEBYTECODE="1")
    try:
        return subprocess.run(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None

def success(result):
    return result is not None and result.returncode == 0

def git(*args):
    return execute(["/usr/bin/git", *args], PROJECT)

def revision(ref="HEAD"):
    result = git("rev-parse", "--verify", ref)
    if not success(result):
        return None
    value = result.stdout.strip()
    return value if len(value) == 40 and all(c in "0123456789abcdef" for c in value) else None

def check_checkout():
    branch = git("symbolic-ref", "--short", "HEAD")
    status = git("status", "--porcelain", "--untracked-files=normal")
    remote = git("remote", "get-url", "origin")
    if not all(success(x) for x in (branch, status, remote)):
        raise RuntimeError("Cannot inspect checkout")
    if branch.stdout.strip() != "main" or status.stdout.strip():
        raise RuntimeError("TV checkout must be clean and on main")
    if remote.stdout.strip().removesuffix(".git") != REMOTE:
        raise RuntimeError("Unexpected Git remote")

def _test():
    result = execute(["/usr/bin/python3", "-m", "unittest", "discover",
                      "-s", "tests", "-v"], PROJECT, timeout=300)
    return {"ok": success(result), "exit_code": result.returncode if result else None}

def run_tests():
    # Project test code is executable, so use the same opt-in as other operations.
    require_write(CONFIRM)
    with operation_lock():
        check_checkout()
        return _test()

def health_check():
    statuses = {unit: service_status(unit) for unit in sorted(UNITS)}
    active = all(x.get("properties", {}).get("ActiveState") == "active"
                 for x in statuses.values())
    http_status = None
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=8) as response:
            http_status = response.status
    except (OSError, urllib.error.URLError):
        pass
    return {"ok": active and http_status == 200, "services": statuses,
            "admin_http_status": http_status,
            "note": "Checks systemd and admin HTTPS, not actual TV playback."}

def _restart(units):
    return success(execute(["/usr/bin/sudo", "-n", "/usr/bin/systemctl",
                            "restart", *units], timeout=90))

def restart_service(unit, confirm):
    validate_unit(unit)
    require_write(confirm)
    with operation_lock():
        if not _restart([unit]):
            return {"ok": False, "stage": "restart"}
        status = service_status(unit)
        return {"ok": status.get("properties", {}).get("ActiveState") == "active",
                "status": status}

def deploy_project(confirm):
    require_write(confirm)
    with operation_lock():
        check_checkout()
        before = revision()
        if not before or not success(git("fetch", "--no-tags", REMOTE, "main")):
            return {"ok": False, "stage": "fetch"}
        target = revision("FETCH_HEAD")
        if not target or not success(git("merge-base", "--is-ancestor", before, target)):
            return {"ok": False, "stage": "non_fast_forward"}
        if before == target:
            return {"ok": True, "changed": False, "revision": before, "health": health_check()}
        if not success(git("merge", "--ff-only", target)):
            return {"ok": False, "stage": "merge", "revision": revision()}
        tests = _test()
        if not tests["ok"]:
            # Do not restart services with untested code. Keep the checkout
            # intact for inspection; never reset or delete production files.
            return {"ok": False, "stage": "tests", "previous": before,
                    "revision": target, "manual_review_required": True, "tests": tests}
        if not _restart(sorted(UNITS)):
            return {"ok": False, "stage": "restart", "previous": before,
                    "revision": target, "manual_review_required": True}
        health = health_check()
        return {"ok": health["ok"], "stage": "health", "changed": True,
                "previous": before, "revision": target, "health": health}
