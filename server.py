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

if __name__ == "__main__":
    mcp.run(transport="stdio")
