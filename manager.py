"""VPS diagnostics and explicitly enabled, fixed TV operations."""
import json
import os
import shutil
import subprocess
import tempfile
import time
import fcntl
import urllib.request
import urllib.error
from contextlib import contextmanager
from pathlib import Path

UNITS = frozenset(("iptv-gateway.service", "iptv-admin.service"))
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "SYSTEMD_PAGER": "cat"}

def run_fixed(args, *, timeout=8):
    try:
        result = subprocess.run(args, capture_output=True, text=True,
                                timeout=timeout, env=ENV, check=False)
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

def service_status(unit, *, timeout=8):
    validate_unit(unit)
    if timeout <= 0:
        return {"available": False, "service": unit}
    properties = ("LoadState", "ActiveState", "SubState", "MainPID", "NRestarts",
                  "MemoryCurrent", "CPUUsageNSec")
    result = run_fixed(["/usr/bin/systemctl", "show", "--no-pager",
                        "--property=" + ",".join(properties), unit], timeout=timeout)
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
    directory = WRITE_FLAG.parent
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

def _test(checkout):
    result = execute(["/usr/bin/python3", "-m", "unittest", "discover",
                      "-s", "tests", "-v"], checkout, timeout=300)
    return {"ok": success(result), "exit_code": result.returncode if result else None}

def _test_revision(target):
    with tempfile.TemporaryDirectory(prefix="vps-manager-") as directory:
        checkout = Path(directory) / "checkout"
        remove = ("worktree", "remove", "--force", str(checkout))
        if not success(git("worktree", "add", "--detach", str(checkout), target)):
            if checkout.exists():
                git(*remove)
            return {"ok": False, "exit_code": None, "stage": "checkout"}
        try:
            result = _test(checkout)
        finally:
            removed = success(git(*remove))
        if not removed:
            return {"ok": False, "exit_code": result["exit_code"], "stage": "cleanup"}
        return result

def run_tests():
    # Project test code is executable, so use the same opt-in as other operations.
    require_write(CONFIRM)
    with operation_lock():
        check_checkout()
        return _test(PROJECT)

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def _admin_http_status(timeout):
    if timeout <= 0:
        return None
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(HEALTH_URL, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as error:
        with error:
            return error.code
    except OSError:
        return None

def _probe_timeout(deadline):
    return 8 if deadline is None else min(8, deadline - time.monotonic())

def health_check(*, deadline=None):
    statuses = {unit: service_status(unit, timeout=_probe_timeout(deadline)) for unit in sorted(UNITS)}
    active = all(x.get("properties", {}).get("ActiveState") == "active"
                 for x in statuses.values())
    http_status = _admin_http_status(_probe_timeout(deadline))
    return {"ok": active and http_status == 200, "services": statuses,
            "admin_http_status": http_status,
            "note": "Checks systemd and admin HTTPS, not actual TV playback."}

def _wait_for_health():
    deadline = time.monotonic() + 30
    health = health_check(deadline=deadline)
    while not health["ok"]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(1, remaining))
        if time.monotonic() >= deadline:
            break
        health = health_check(deadline=deadline)
    return health

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

def _verified_revision(revision_file):
    try:
        return revision_file.read_text(encoding="ascii", errors="replace").strip()
    except FileNotFoundError:
        return None

def _record_verified_revision(revision_file, target):
    with tempfile.TemporaryDirectory(dir=revision_file.parent, prefix="revision-") as directory:
        pending = Path(directory) / revision_file.name
        pending.write_text(target + "\n", encoding="ascii")
        pending.replace(revision_file)

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
        revision_file = WRITE_FLAG.with_name("verified-revision")
        changed = before != target
        if not changed and _verified_revision(revision_file) == target:
            health = health_check()
            return {"ok": health["ok"], "changed": False, "revision": target, "health": health}
        tests = _test_revision(target)
        if not tests["ok"]:
            return {"ok": False, "stage": "tests", "previous": before,
                    "revision": target, "manual_review_required": True, "tests": tests}
        check_checkout()
        if revision() != before:
            return {"ok": False, "stage": "checkout", "previous": before, "revision": target}
        # A failed or interrupted deployment must be retried even when HEAD matches main.
        revision_file.unlink(missing_ok=True)
        if changed and not success(git("merge", "--ff-only", target)):
            return {"ok": False, "stage": "merge", "revision": revision()}
        if revision() != target:
            return {"ok": False, "stage": "checkout", "previous": before, "revision": target}
        if not _restart(sorted(UNITS)):
            return {"ok": False, "stage": "restart", "previous": before,
                    "revision": target, "manual_review_required": True}
        health = _wait_for_health()
        if health["ok"]:
            _record_verified_revision(revision_file, target)
        return {"ok": health["ok"], "stage": "health", "changed": changed,
                "previous": before, "revision": target, "health": health}
