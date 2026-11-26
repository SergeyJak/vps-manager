# VPS Manager

Local MCP server for Linux VPS administration. Initial scope: IPTV gateway and admin service.

## Security model

- Local stdio transport only; no listening TCP port, no unauthenticated endpoint.
- Run as an unprivileged account on the VPS. Never run as root.
- No arbitrary shell, filesystem reads, credentials or raw journal messages. Fixed TV operations require local opt-in.
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

Tools: `host_metrics`, `service_status`, `journal_summary`. Journal summary is deliberately bounded and does not include raw messages. Additional tools: health_check, run_tests, restart_service, deploy_project.

ChatGPT custom remote MCP connections require an eligible account/workspace. This stdio server is not directly connectable from a normal ChatGPT Plus chat. For future remote integration, use an authenticated secure tunnel and approved workspace, or build a separately authenticated web interface for ChatGPT Work. Do not expose this server publicly without authentication.

## Next steps

## Enable TV operations (optional)

Default is read-only. On the VPS, as the existing unprivileged TV deployment account:

    mkdir -p ~/.config/vps-manager
    chmod 700 ~/.config/vps-manager
    touch ~/.config/vps-manager/enable-writes

Remove that flag to disable operations. Restart and deploy also require explicit user approval in the MCP client and a confirmation parameter. The parameter is not an authentication secret. SSH is the authentication boundary.

For noninteractive restart, have the server administrator verify existing sudo privileges with `sudo -l`. If needed, use `visudo` to add narrowly scoped NOPASSWD rules for exact commands only: /usr/bin/systemctl restart iptv-gateway.service; /usr/bin/systemctl restart iptv-admin.service; /usr/bin/systemctl restart iptv-admin.service iptv-gateway.service. Never grant generic systemctl or unrestricted sudo. The TV units must run as an unprivileged account, not root, before granting this permission. Verify their effective User and unit ownership first.

Deploy checks a clean main checkout and expected HTTPS origin, fetches main, refuses non-fast-forward changes, runs tests, restarts only the two TV units and checks HTTPS. It does not automatically roll back code or SQLite migrations. On failure it reports the previous and target revision; review manually before any further restart. Test output and raw journal messages are not returned; this is intentional to prevent accidental disclosure of TV subscription credentials. Health check does not establish actual video delivery.

No public port or remote HTTP MCP is added.
