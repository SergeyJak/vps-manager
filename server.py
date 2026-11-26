from mcp.server.fastmcp import FastMCP
import manager

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
def run_tests() -> dict:
    """Run the TV project's unittest suite when VPS operations are enabled."""
    return manager.run_tests()

@mcp.tool()
def restart_service(unit: str, confirm: str) -> dict:
    """Restart one allowed IPTV service. Obtain user approval before calling; confirm=CONFIRM_TV_CHANGE."""
    return manager.restart_service(unit, confirm)

@mcp.tool()
def deploy_project(confirm: str) -> dict:
    """Fast-forward TV main, run tests, then restart both services and check health. Obtain user approval first; confirm=CONFIRM_TV_CHANGE."""
    return manager.deploy_project(confirm)

if __name__ == "__main__":
    mcp.run(transport="stdio")
