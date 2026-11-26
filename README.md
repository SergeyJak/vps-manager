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

## IPTV monitoring

- `iptv_stability(days=1, end_at=None)` reads retained IPTV incidents and
  client delivery episodes. Supported windows are 1, 7 and 30 days.
  Results include coverage, counts, recovered episode durations and up to
  20 affected channels and recent events per list. Channels use stable IDs.
  The gateway's recording state and dropped-event counter apply at query
  time and reset when the process restarts. An unavailable database is
  reported separately from zero recorded incidents. The episode reset
  boundary does not apply to the separate incident journal.
- `resource_history(days=1, limit=288, end_at=None)` reads the latest samples
  within the window and reports its peaks and observed revision changes.
  CPU time and network bytes are cumulative counters. Compare counter deltas
  between samples and discard resets. A checkout revision can differ from the
  verified deployment revision. Changes are detected at sample time.
- `probe_playback()` checks that the public HLS manifest advances and a
  fresh media segment yields data. It uses one locally configured channel.
  It can warm a stream, occupy a slot and appear in viewing telemetry. It
  does not test a TV's decoder. Use a dedicated test channel where possible.

Use the optional UTC Unix `end_at` to compare equal periods before and
after a deployment. Segment error fractions use recorded segment 404s
and successful segment requests, excluding manifest errors. Derived rates
are omitted when sessions cross the window because individual request
timestamps are not retained. Viewing time is estimated from gateway requests.
The 30-day incident window is also subject to the IPTV database's incident cap
and manual clearing.

Monitoring uses fixed paths under `~/Tvtest` and the existing local gateway
status endpoint on port 8787. The MCP calls accept no filesystem path,
credentials or URL and expose no raw logs, viewer identifiers or response
bodies. Retain the deployment account's existing private access to the
telemetry and profile files.

### Start resource sampling

Install the included user units as the existing VPS account. They assume
the repository and its virtual environment are in `~/vps-manager`.

    install -d -m 700 ~/.config/systemd/user
    install -m 644 systemd/vps-manager-sample.{service,timer} ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now vps-manager-sample.timer
    systemctl --user start vps-manager-sample.service

The sampler runs every five minutes and retains 30 days in
`~/.config/vps-manager/monitor-history.sqlite3`. Continuous collection
requires the user systemd manager to remain active between login sessions.
Check `loginctl show-user "$USER" -p Linger` and have the server administrator
enable lingering if needed.

If the VPS already runs cron, use an entry in the deployment account's
crontab instead of the user timer. Replace the example home directory with
the real absolute path. Enable exactly one scheduler.

    */5 * * * * /home/ACCOUNT/vps-manager/.venv/bin/python /home/ACCOUNT/vps-manager/monitor_history.py

### Opt into the playback check

Create `~/.config/vps-manager/playback-probe.json` with mode `0600`.
Set `token` to the selected profile's TV token and `channel_id` to the
20-character channel ID from that profile's playlist. Store both locally
on the VPS and leave the file absent until a channel is chosen. The HLS
check runs only on request, and downloads at most 64 KiB of one segment.

Reconnect the MCP client after updating the manager so it registers the new
tools.

## Enable TV operations (optional)

Default is read-only. On the VPS, as the existing unprivileged TV deployment account:

    mkdir -p ~/.config/vps-manager
    chmod 700 ~/.config/vps-manager
    touch ~/.config/vps-manager/enable-writes

Remove that flag to disable operations. Restart and deploy also require explicit user approval in the MCP client and a confirmation parameter. The parameter is not an authentication secret. SSH is the authentication boundary.

For noninteractive restart, have the server administrator verify existing sudo privileges with `sudo -l`. If needed, use `visudo` to add narrowly scoped NOPASSWD rules for exact commands only: /usr/bin/systemctl restart iptv-gateway.service; /usr/bin/systemctl restart iptv-admin.service; /usr/bin/systemctl restart iptv-admin.service iptv-gateway.service. Never grant generic systemctl or unrestricted sudo. The TV units must run as an unprivileged account, not root, before granting this permission. Verify their effective User and unit ownership first.

Deploy checks a clean `main` checkout and the expected HTTPS origin, fetches `main`, and refuses non-fast-forward changes. It runs the target revision's tests in a temporary Git worktree before updating the live checkout. After restarting the two TV units, it retries the health check for up to 30 seconds. The HTTPS probe checks `/admin/` directly and reports the HTTP status, including redirects and errors. Health check covers systemd and admin HTTPS. Actual video delivery requires a separate playback check.

Deploy records the verified revision in `~/.config/vps-manager/verified-revision` after a successful restart and health check. If verification is pending, a retry runs the deployment steps even when the checkout already matches the target. This also applies on the first run with an existing checkout. A test failure leaves the live checkout untouched. After a restart or health failure, the checkout can already be at the target, and code and SQLite migrations require manual rollback if needed. To protect TV subscription credentials, tool results include test exit codes and journal counts while keeping their raw output on the server.

No public port or remote HTTP MCP is added.
