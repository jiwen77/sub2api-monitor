# sub2api-monitor

A passive monitoring and Telegram alerting tool for [Sub2API](https://github.com/Wei-Shaw/sub2api).

`sub2api-monitor` does **not** send synthetic requests to upstream providers. It only reads Sub2API's own PostgreSQL tables and reports changes that Sub2API has already observed.

## Features

- **Account status alerts**
  - Summarizes accounts by `platform/plan`, for example `openai/free: normal 2/2`.
  - Reports state changes for active/error/rate-limited/overloaded/temporarily-unschedulable/expired accounts.
  - Redacts account identifiers by default.

- **Upstream error alerts**
  - Reads Sub2API `ops_error_logs`.
  - Defaults to provider/upstream `429` and `5xx` alerts.
  - Filters out common client-side, API-key, authentication, request-body, and network-looking errors.

- **Readable Telegram cards**
  - Uses compact lines, emoji severity hints, bold section headers, and shortened error summaries.

- **Daily usage report**
  - Reads Sub2API `usage_logs`.
  - Sends previous-day totals and current-day-to-now totals.
  - Includes request count, token totals, cost, account-plan breakdown, and top models.

- **Interactive management script**
  - Configure Telegram.
  - Test notifications.
  - Print current account status.
  - Install/update from GitHub.
  - Install and manage the systemd service.

## Requirements

- Linux host running Sub2API with PostgreSQL.
- Docker CLI access to the Sub2API PostgreSQL container.
- Python 3.11+ recommended.
- `git` and `rsync` for GitHub self-update.
- A Telegram bot token and chat ID for notifications.

Default assumptions can be changed in `/etc/sub2api-monitor/config.env`:

- Sub2API directory: `/opt/sub2api`
- PostgreSQL container: `sub2api-postgres`
- Sub2API env file: `/opt/sub2api/.env`

## Quick start

Download the management script and use menu option 1 to install the full project from GitHub:

```bash
curl -fsSL https://raw.githubusercontent.com/jiwen77/sub2api-monitor/main/monitor.sh -o /tmp/sub2api-monitor.sh
sudo bash /tmp/sub2api-monitor.sh
```

Recommended first-run flow:

1. `从 GitHub 安装/更新项目文件`
2. `配置 Telegram`
3. `发送 Telegram 测试`
4. `查看当前账号状态（不通知）`
5. `安装并启动 systemd 服务`

After installation, open the menu with:

```bash
sudo /opt/sub2api-monitor/monitor.sh
```

## Self-update

Interactive update:

```bash
sudo /opt/sub2api-monitor/monitor.sh
# choose menu option 1
```

Non-interactive update:

```bash
sudo /opt/sub2api-monitor/monitor.sh --update
```

By default, menu option 1 uses:

```env
UPDATE_REPO_URL=https://github.com/jiwen77/sub2api-monitor.git
UPDATE_REF=main
```

For forks or pinned releases:

```bash
sudo UPDATE_REPO_URL=https://github.com/yourname/sub2api-monitor.git \
  UPDATE_REF=main \
  /opt/sub2api-monitor/monitor.sh --update
```

## Manual commands

```bash
python3 /opt/sub2api-monitor/sub2api_monitor.py --config /etc/sub2api-monitor/config.env account-summary
python3 /opt/sub2api-monitor/sub2api_monitor.py --config /etc/sub2api-monitor/config.env run-once --notify
python3 /opt/sub2api-monitor/sub2api_monitor.py --config /etc/sub2api-monitor/config.env daily --notify
python3 /opt/sub2api-monitor/sub2api_monitor.py --config /etc/sub2api-monitor/config.env daemon
```

Systemd service:

```bash
sudo systemctl enable --now sub2api-monitor.service
sudo systemctl status sub2api-monitor.service
sudo journalctl -u sub2api-monitor.service -f
```

## Configuration

Configuration file:

```text
/etc/sub2api-monitor/config.env
```

Important options:

| Option | Default | Description |
| --- | --- | --- |
| `SUB2API_DIR` | `/opt/sub2api` | Sub2API deployment directory. The monitor reads its `.env` for PostgreSQL credentials. |
| `POSTGRES_CONTAINER` | `sub2api-postgres` | PostgreSQL container name. |
| `TELEGRAM_BOT_TOKEN` | empty | Telegram bot token. |
| `TELEGRAM_CHAT_ID` | empty | Telegram chat/channel/user ID. |
| `TELEGRAM_PARSE_MODE` | `HTML` | Telegram formatting mode for readable bold headings and code labels. |
| `POLL_INTERVAL_SECONDS` | `60` | Daemon polling interval. |
| `ERROR_LOOKBACK_MINUTES` | `30` | Lookback window for new upstream errors. |
| `ERROR_COOLDOWN_SECONDS` | `600` | Per-error-group cooldown. |
| `UPSTREAM_ALLOWED_STATUS_CODES` | `429,500-599` | Upstream HTTP statuses that should alert. |
| `REDACT_IDENTIFIERS` | `true` | Redact account names/emails in alerts. |
| `DAILY_REPORT_HOUR` | `0` | Local hour for daily report. |
| `DAILY_REPORT_MINUTE` | `0` | Local minute for daily report. |

See [`config.env.example`](./config.env.example) for the full set of options.

## Security model

- The monitor is read-only from Sub2API's perspective.
- It does not modify Sub2API tables.
- It does not send active probes to upstream providers.
- Runtime configuration and secrets live outside the Git repository in `/etc/sub2api-monitor/config.env`.
- The repository only includes `config.env.example`; do not commit real Telegram tokens or database credentials.

## Development

Run checks locally:

```bash
bash -n monitor.sh
python3 -m py_compile sub2api_monitor.py
PYTHONPATH=. python3 -m unittest discover -v -s tests
```

## License

MIT License. See [LICENSE](./LICENSE).
