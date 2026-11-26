from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP
import iptv_monitoring
import manager
import monitor_history
import playback_probe

mcp = FastMCP("vps-manager")

@mcp.tool()
def host_metrics() -> dict:
    """Read Linux host resource metrics."""
    return manager.host_metrics()

@mcp.tool()
def service_status(unit: str) -> dict:
    """Read status of iptv-gateway.service or iptv-admin.service."""
    return manager.service_status(unit)

@mcp.tool()
def journal_summary(unit: str, minutes: int = 60) -> dict:
    """Summarize recent journal priorities, without returning sensitive messages."""
    return manager.journal_summary(unit, minutes)

@mcp.tool()
def health_check() -> dict:
    """Check TV systemd services and public admin HTTPS, not playback."""
    return manager.health_check()

@mcp.tool()
def iptv_stability(days: int = 1, end_at: int | None = None) -> dict:
    """Read IPTV incidents, delivery episodes and telemetry coverage for 1, 7 or 30 days.

    Optionally end the window at a UTC Unix timestamp to compare periods.
    """
    return iptv_monitoring.stability(manager.PROJECT, days, end_at)

@mcp.tool()
def resource_history(days: int = 1, limit: int = 288, end_at: int | None = None) -> dict:
    """Read VPS and IPTV resource samples for 1, 7 or 30 days, with revision changes.

    Optionally end the window at a UTC Unix timestamp. Returns at most 500 latest samples.
    """
    return monitor_history.read_history(
        manager.WRITE_FLAG.with_name("monitor-history.sqlite3"), days, limit, end_at)

@mcp.tool()
def probe_playback() -> dict:
    """Check public HLS manifest progression and download part of a fresh segment.

    This opt-in probe requests one locally configured channel as a viewer.
    """
    target = urlsplit(manager.HEALTH_URL)
    return playback_probe.probe(manager.WRITE_FLAG.parent, f"{target.scheme}://{target.netloc}")

@mcp.tool()
def run_tests() -> dict:
    """Run the TV project's unittest suite when VPS operations are enabled."""
    return manager.run_tests()

@mcp.tool()
def restart_service(unit: str, confirm: str) -> dict:
    """Restart one allowed IPTV service. Obtain user approval before calling; confirm=CONFIRM_TV_CHANGE."""
    return manager.restart_service(unit, confirm)

@mcp.tool()
def deploy_project(confirm: str) -> dict:
    """Test the target revision, fast-forward TV main, then restart both services and verify health. Obtain user approval first; confirm=CONFIRM_TV_CHANGE."""
    return manager.deploy_project(confirm)

if __name__ == "__main__":
    mcp.run(transport="stdio")
