# sub2api-monitor

A passive monitoring and Telegram alerting tool for [Sub2API](https://github.com/Wei-Shaw/sub2api).

`sub2api-monitor` does **not** send synthetic requests to upstream providers. It only reads Sub2API's own PostgreSQL tables and reports changes that Sub2API has already observed.

## Features

- **Account status alerts**
  - Summarizes accounts by `platform/plan`, for example `openai/free: normal 2/2`.
  - Can send the current account status on demand, even when nothing changed.
  - Automatic change alerts only show the accounts that changed, so old abnormalities are not repeated every time.
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

- **Telegram bot commands**
  - While the daemon is running, send `/status` to receive the current account status immediately.
  - Supports `/daily`, `/ping`, and `/help`.
  - Registers Telegram slash-command suggestions with `setMyCommands` so typing `/` can show completions.
  - Only authorized chat IDs are served.

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

1. `安装/更新程序（从 GitHub 拉取）`
2. `配置 Telegram 通知`
3. `测试 Telegram 通知`
4. `查看账号状态（只显示，不发 TG）`
5. `发送账号状态到 TG（立即发送）`
6. `后台启动/重启监控（推荐）`

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



## Menu option guide

Most users start with options 1-5 and 10; use option 13 when you want guided configuration changes:

| Option | Use it for | Notes |
| --- | --- | --- |
| 1 | Install or update from GitHub | Pulls the latest project files, keeps your existing config, then reloads the new menu. |
| 2 | Configure Telegram notifications | Saves bot token/chat ID in `/etc/sub2api-monitor/config.env`. |
| 3 | Test Telegram notifications | Verifies the bot can send messages. |
| 4 | View account status locally | Prints status only; does not send Telegram. |
| 5 | Send account status to Telegram now | Always sends the current snapshot. |
| 6 | Run alert checks once | Sends Telegram only if account state changed or upstream errors are found. |
| 7 | Preview daily report locally | Prints the report only. |
| 8 | Send daily report to Telegram now | Always sends the current daily report. |
| 9 | Run monitor temporarily in the foreground | Mainly for debugging; closing SSH stops it. |
| 10 | Start/restart background monitoring | Recommended production mode; keeps running after SSH disconnects and after reboot. |
| 11 | View background monitor status/logs | Useful when checking whether it is running normally. |
| 12 | Stop background monitoring and disable autostart | Use this when you do not want automatic alerts anymore. |
| 13 | Change common settings interactively | Guided prompts for polling, Telegram commands, alerts, daily reports, and Sub2API/Postgres paths. |
| 14 | Manually edit config file | Opens `/etc/sub2api-monitor/config.env` in `nano` or `$EDITOR`. |
| 15 | Uninstall program files | Keeps config/state by default. |

## Telegram commands

When the systemd service or `daemon` command is running, the bot can also receive commands from Telegram. The monitor registers the command list with Telegram automatically on startup and when you run `测试 Telegram 通知`, so typing `/` in the chat can show command completions.

| Command | Description |
| --- | --- |
| `/status` | Send the current account status immediately. |
| `/accounts` | Alias for `/status`. |
| `/daily` | Send the previous-day/current-day usage report. |
| `/ping` | Check whether the daemon is receiving commands. |
| `/help` | Show command help. |

By default, commands are only accepted from `TELEGRAM_CHAT_ID`. To authorize multiple chats, set:

```env
TELEGRAM_ALLOWED_CHAT_IDS=123456789,-1001234567890
```

The daemon drops pending Telegram updates on first startup by default so old `/start` or `/status` messages are not replayed. Change this with `TELEGRAM_DROP_PENDING_UPDATES=false` if you want to process queued commands after first startup.

## Manual commands

```bash
# Print the current account snapshot without sending Telegram.
python3 /opt/sub2api-monitor/sub2api_monitor.py --config /etc/sub2api-monitor/config.env account-summary

# Force-send the current account snapshot to Telegram.
python3 /opt/sub2api-monitor/sub2api_monitor.py --config /etc/sub2api-monitor/config.env account-summary --notify

# Run the same alert rules once: account changes and upstream errors only.
python3 /opt/sub2api-monitor/sub2api_monitor.py --config /etc/sub2api-monitor/config.env run-once --notify

# Force-send the daily usage report.
python3 /opt/sub2api-monitor/sub2api_monitor.py --config /etc/sub2api-monitor/config.env daily --notify

# Register Telegram slash-command suggestions.
python3 /opt/sub2api-monitor/sub2api_monitor.py --config /etc/sub2api-monitor/config.env setup-telegram-commands

# Keep monitoring in the foreground.
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
| `TELEGRAM_COMMANDS_ENABLED` | `true` | Enable `/status`, `/daily`, `/ping`, and `/help` command polling in daemon mode. |
| `TELEGRAM_COMMAND_POLL_INTERVAL_SECONDS` | `5` | How often daemon mode checks Telegram commands. |
| `TELEGRAM_ALLOWED_CHAT_IDS` | empty | Optional comma-separated allowlist; defaults to `TELEGRAM_CHAT_ID`. |
| `POLL_INTERVAL_SECONDS` | `60` | Daemon polling interval. |
| `ERROR_LOOKBACK_MINUTES` | `30` | Lookback window for new upstream errors. |
| `ERROR_COOLDOWN_SECONDS` | `600` | Per-error-group cooldown. |
| `UPSTREAM_ALLOWED_STATUS_CODES` | `429,500-599` | Upstream HTTP statuses that should alert. |
| `REDACT_IDENTIFIERS` | `true` | Redact account names/emails in alerts. |
| `DAILY_REPORT_HOUR` | `0` | Local hour for daily report. |
| `DAILY_REPORT_MINUTE` | `0` | Local minute for daily report. |

You can edit these with menu option `13) 交互式修改配置项`; option `14) 手动编辑配置文件（nano）` remains available for advanced/manual edits.

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
