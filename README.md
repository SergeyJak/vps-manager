# VPS Manager

Read-only local MCP server for Linux VPS administration. Initial scope: IPTV gateway and admin service.

## Security model

- Local stdio transport only; no listening TCP port, no unauthenticated endpoint.
- Run as an unprivileged account on the VPS. Never run as root.
- No arbitrary shell, filesystem reads, credentials, raw journal messages, deploy or restart commands.
- Fixed allowlist of systemd units. Journal summaries expose counts and timestamps only.
- Repository is public: never commit credentials or actual server data.

## Install on VPS

    git clone https://github.com/SergeyJak/vps-manager.git
    cd vps-manager
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    .venv/bin/python -m unittest discover -s tests -v

Python 3.10+ required. The account needs permission to read the selected systemd journal; if unavailable, the tool reports a failure. Do not grant root or broad sudo rights.

## Connect from Codex or another local MCP client

Use stdio command `/absolute/path/vps-manager/.venv/bin/python` with argument `/absolute/path/vps-manager/server.py` on the VPS itself. From a workstation, an SSH stdio transport may be used with existing key authentication and verified host keys (no password or key in this repository):

    codex mcp add vps-manager -- ssh -T -o BatchMode=yes YOUR_VPS_LOGIN@YOUR_VPS_HOST /absolute/path/vps-manager/.venv/bin/python /absolute/path/vps-manager/server.py

The MCP client must be able to execute SSH. Verify access using a normal SSH login first.

Tools: `host_metrics`, `service_status`, `journal_summary`. Journal summary is deliberately bounded and does not include raw messages. No write actions are implemented.

ChatGPT custom remote MCP connections require an eligible account/workspace. This stdio server is not directly connectable from a normal ChatGPT Plus chat. For future remote integration, use an authenticated secure tunnel and approved workspace, or build a separately authenticated web interface for ChatGPT Work. Do not expose this server publicly without authentication.

## Next steps

Review actual service names and permissions on VPS; verify metrics and journal counts. Add narrowly scoped, separately approved restart/deploy operations only after establishing authenticated access and testing rollback.
