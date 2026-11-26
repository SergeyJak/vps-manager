"""Read the IPTV telemetry database and the gateway's local status."""
import json
import re
import sqlite3
import statistics
import time
from contextlib import closing
from http.client import HTTPException
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

DAYS = (1, 7, 30)
CHANNEL_ID = re.compile(r"(?:p[23]:)?[a-f0-9]{20}")
TOKEN = re.compile(r"[A-Za-z0-9_-]{12,128}")
KINDS = ("upstream_error", "segment_slow", "stream_stalled", "stream_recovered",
         "client_error", "startup_timeout", "cdn_decision")
SEGMENT_FAILURES = ("HTTP_404_STREAM_EXPIRED", "HTTP_404_SEGMENT_CACHE_MISS")
CDN_REASONS = ("START", "SOURCE_RESTARTED", "SOURCE_UNAVAILABLE", "PUBLISH_STALL",
               "NO_READY_BACKUP", "SWITCH_COOLDOWN", "HOME_RECOVERED", "SOURCE_AVAILABLE")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def direct_opener():
    return build_opener(ProxyHandler({}), NoRedirect())


def gateway_snapshot(project):
    """Project only runtime scalars from the same loopback endpoint as the admin."""
    try:
        profiles = json.loads((project / ".iptv_profiles/profiles.json").read_text(encoding="utf-8"))
        first = next(row for row in profiles["profiles"] if row["slot"] == 1)
        token, credential = first["token"], profiles["monitor_token"]
        if not TOKEN.fullmatch(token) or not TOKEN.fullmatch(credential):
            raise ValueError
        request = Request(f"http://127.0.0.1:8787/{token}/status?scope=cabinets")
        request.add_unredirected_header("Authorization", "Bearer " + credential)
        with direct_opener().open(request, timeout=2) as response:
            payload = json.loads(response.read(8 * 1024 * 1024))
        cabinets = payload["cabinet_statuses"]
        if not cabinets or any(not isinstance(row.get("streams"), list) or
                               type(row.get("telemetry_enabled")) is not bool for row in cabinets.values()):
            raise ValueError
        dropped = [row.get("telemetry_dropped") for row in cabinets.values()]
        return {
            "available": True,
            "telemetry_enabled": any(row["telemetry_enabled"] for row in cabinets.values()),
            # The cabinets share the recorder and therefore its process-wide counter.
            "telemetry_dropped": max(dropped) if all(type(n) is int and n >= 0 for n in dropped) else None,
            "active_streams": sum(len(row["streams"]) for row in cabinets.values()),
        }
    except (OSError, HTTPException, ValueError, TypeError, AttributeError, KeyError, StopIteration):
        return {"available": False, "telemetry_enabled": None, "telemetry_dropped": None, "active_streams": None}


def read_only(path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    return db


def window(days, end_at=None, *, now=None):
    now = int(time.time()) if now is None else now
    if type(days) is not int or days not in DAYS:
        raise ValueError("days must be 1, 7, or 30")
    end = now if end_at is None else end_at
    if type(end) is not int or not 0 <= end <= now:
        raise ValueError("end_at must be a UTC Unix timestamp no later than now")
    return max(0, end - days * 86400), end


def safe_channel(value):
    return value if isinstance(value, str) and CHANNEL_ID.fullmatch(value) else None


def _episodes(db, start, end, tables):
    if not {"delivery_monitor", "delivery_episodes"} <= tables:
        return None, [], [], []
    installed = db.execute("SELECT installed FROM delivery_monitor WHERE id=1").fetchone()
    if installed is None:
        return None, [], [], []
    since = installed[0]
    if "delivery_reset" in tables:
        reset = db.execute("SELECT MAX(reset_at) FROM delivery_reset").fetchone()[0]
        if reset:
            since = max(since, reset + 1)
    args = max(start, since - 1), end
    grouped = db.execute(
        """SELECT channel_id, COUNT(*) AS count FROM delivery_episodes
           WHERE confirmed > ? AND confirmed <= ? GROUP BY channel_id""", args).fetchall()
    events = []
    for row in db.execute(
        """SELECT channel_id, reason, started, confirmed, recovered, recovery_slot
           FROM delivery_episodes WHERE confirmed > ? AND confirmed <= ?
           ORDER BY confirmed DESC, id DESC LIMIT 20""", args):
        recovered = row["recovered"] if row["recovered"] is not None and row["recovered"] <= end else None
        events.append({
            "channel_id": safe_channel(row["channel_id"]),
            "reason": row["reason"] if row["reason"] in ("playlist", "requests", "startup") else "other",
            "started_at": row["started"], "confirmed_at": row["confirmed"],
            "recovered_at": recovered,
            "duration_seconds": max(0, recovered - row["started"]) if recovered is not None else None,
            "recovery_slot": row["recovery_slot"] if recovered is not None and row["recovery_slot"] in (1, 2, 3) else None,
        })
    durations = [row[0] for row in db.execute(
        """SELECT recovered - started FROM delivery_episodes
           WHERE confirmed > ? AND confirmed <= ? AND recovered BETWEEN started AND ?""", (*args, end))]
    return since, grouped, events, durations


def stability(project, days=1, end_at=None, *, now=None):
    start, end = window(days, end_at, now=now)
    path = project / ".iptv_state/telemetry.sqlite3"
    coverage = {"database": "missing", "gateway_now": gateway_snapshot(project),
                "delivery_episodes_since": None, "first_retained_incident_at": None,
                "last_retained_incident_at": None}
    report = {"start_at": start, "end_at": end, "coverage": coverage, "totals": None,
              "channels": [], "recent_incidents": [], "recent_episodes": []}
    try:
        with closing(read_only(path)) as db, db:
            db.execute("BEGIN")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"incidents", "viewing_sessions"} <= tables:
                coverage["database"] = "unsupported_schema"
                return report
            coverage["first_retained_incident_at"], coverage["last_retained_incident_at"] = db.execute(
                "SELECT MIN(occurred_at), MAX(occurred_at) FROM incidents").fetchone()
            counts = {kind: 0 for kind in (*KINDS, "other")}
            channels = {}
            for row in db.execute("""SELECT channel_id, kind, COUNT(*) AS count FROM incidents
                                     WHERE occurred_at > ? AND occurred_at <= ? GROUP BY channel_id, kind""",
                                  (start, end)):
                kind = row["kind"] if row["kind"] in KINDS else "other"
                counts[kind] += row["count"]
                channel = safe_channel(row["channel_id"])
                if channel:
                    channel_counts = channels.setdefault(channel, {"incidents": {}, "delivery_episodes": None})["incidents"]
                    channel_counts[kind] = channel_counts.get(kind, 0) + row["count"]
            since, episode_counts, episodes, durations = _episodes(db, start, end, tables)
            coverage["delivery_episodes_since"] = since
            for row in episode_counts:
                channel = safe_channel(row["channel_id"])
                if channel:
                    channels.setdefault(channel, {"incidents": {}, "delivery_episodes": 0})["delivery_episodes"] = row["count"]
            if since is not None:
                for channel in channels.values():
                    if channel["delivery_episodes"] is None:
                        channel["delivery_episodes"] = 0
            watch_seconds, requests = db.execute(
                """SELECT COALESCE(SUM(estimated_seconds), 0), COALESCE(SUM(segment_requests), 0)
                   FROM viewing_sessions WHERE started_at > ? AND last_seen <= ?""", (start, end)).fetchone()
            overlaps = db.execute(
                """SELECT EXISTS(SELECT 1 FROM viewing_sessions WHERE last_seen > ? AND started_at <= ?
                                 AND (started_at <= ? OR last_seen > ?))""", (start, end, start, end)).fetchone()[0]
            failures = db.execute(
                """SELECT COUNT(*) FROM incidents WHERE occurred_at > ? AND occurred_at <= ?
                   AND kind='client_error' AND detail IN (?, ?)""", (start, end, *SEGMENT_FAILURES)).fetchone()[0]
            recent = []
            for row in db.execute(
                """SELECT occurred_at, channel_id, kind, detail, duration_seconds FROM incidents
                   WHERE occurred_at > ? AND occurred_at <= ? ORDER BY occurred_at DESC, id DESC LIMIT 20""", (start, end)):
                recent.append({"occurred_at": row["occurred_at"], "channel_id": safe_channel(row["channel_id"]),
                               "kind": row["kind"] if row["kind"] in KINDS else "other",
                               "reason": _incident_reason(row["detail"]),
                               "duration_seconds": row["duration_seconds"]})
    except sqlite3.Error:
        if path.exists():
            coverage["database"] = "unreadable"
        return report
    coverage["database"] = "ready"
    confirmed = sum(row["count"] for row in episode_counts) if since is not None else None
    # Sessions crossing the report boundary do not have individual request timestamps.
    # Keep the counts, but suppress derived ratios if those sessions overlap the window.
    coverage["sessions_cross_window_boundary"] = bool(overlaps)
    report["totals"] = {
        "incidents": sum(counts.values()), "incidents_by_kind": counts,
        "delivery_episodes": confirmed, "estimated_viewing_seconds_in_complete_sessions": watch_seconds,
        "successful_segment_requests_in_complete_sessions": requests, "recorded_segment_failures": failures,
        "client_segment_failure_fraction": round(failures / (requests + failures), 6) if requests and not overlaps else None,
        "delivery_episodes_per_estimated_viewing_hour": (
            round(confirmed * 3600 / watch_seconds, 3)
            if confirmed is not None and watch_seconds > 0 and since <= start + 1 and not overlaps else None),
        "recovered_delivery_episodes": len(durations) if since is not None else None,
        "median_recovery_seconds": statistics.median(durations) if durations else None,
        "longest_recovery_seconds": max(durations, default=None),
    }
    ranked = sorted(channels.items(), key=lambda x: (-(x[1]["delivery_episodes"] or 0),
                                                     -sum(x[1]["incidents"].values()), x[0]))
    report.update(channels=[dict(values, channel_id=channel) for channel, values in ranked[:20]],
                  recent_incidents=recent, recent_episodes=episodes)
    return report


def _incident_reason(value):
    if not isinstance(value, str):
        return None
    if value in (*SEGMENT_FAILURES, *CDN_REASONS, "HTTP_503_STREAM_LIMIT", "HTTP_503_MANIFEST_WARMUP",
                 "MANIFEST_WARMUP_TIMEOUT"):
        return value
    match = re.match(r"(?:HTTP[ _])([1-5][0-9]{2})(?:\b|_)", value)
    return "HTTP_" + match[1] if match else None
