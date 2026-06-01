# sub2api-monitor

_A passive Sub2API monitoring daemon that sends privacy-aware Telegram alerts._

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](#requirements)
[![Sub2API](https://img.shields.io/badge/Sub2API-compatible-green.svg)](https://github.com/Wei-Shaw/sub2api)

`sub2api-monitor` watches an existing [Sub2API](https://github.com/Wei-Shaw/sub2api) PostgreSQL database and sends concise Telegram notifications. It is intentionally passive: it does not probe upstream providers, does not mutate Sub2API tables, and keeps operational secrets outside the Git repository.

## 🚀 Highlights

| Capability | What it does |
| --- | --- |
| Account alerts | Detects account additions, removals, group changes, and health changes such as error, rate limit, overload, temporary unschedulable, and expiry states |
| Recharge alerts | Detects increases in `users.total_recharged` and reports the recharge delta, cumulative recharge, and current balance |
| Upstream error alerts | Reads `ops_error_logs` and reports actionable provider-side `429` and `5xx` failures |
| Exit-network alerts | Separately reports proxy, DNS, timeout, TLS, and connection failures without exposing proxy credentials |
| Daily usage reports | Summarizes request count, token usage, cost, account-plan breakdowns, and top models |
| Telegram commands | Supports `/status`, `/accounts`, `/daily`, `/ping`, and `/help` while the daemon is running |
| Interactive installer | Provides a menu-driven shell script for install, update, configuration, service management, and smoke tests |

## 🔒 Privacy and security

The project is designed for public GitHub hosting and privacy-aware operations.

| Area | Behavior |
| --- | --- |
| Secrets | Real Telegram tokens, chat IDs, database passwords, and Sub2API `.env` values stay outside the repository |
| User identifiers | Account and user names/emails are redacted by default with `REDACT_IDENTIFIERS=true` |
| Proxy data | Proxy alert cards show safe labels such as name, ID, protocol, and status, but not host, username, or password |
| Low-level IDs | Request IDs and raw ops-log IDs are omitted from default Telegram alert text |
| Database access | The monitor only reads Sub2API tables through `psql`; it does not write application data |
| Examples | README examples use placeholders or masked identifiers only |

Example recharge alert with redaction enabled:

```text
💰 Sub2API 用户充值
2026-06-01 13:45:20 CST
新增 1 笔 / 合计 +50

充值明细
• #42 us***@ex*** (user, active)
  充值：+50 · 累计：150 · 余额：75
  更新时间：06-01 13:45
```

## 🧭 How it works

```mermaid
flowchart LR
    accTitle: Passive monitoring flow
    accDescr: sub2api-monitor reads Sub2API PostgreSQL tables, stores local baselines, and sends Telegram notifications without probing upstream APIs or modifying Sub2API data.

    sub2api["Sub2API database"] --> monitor["sub2api-monitor"]
    state["Local state file"] <--> monitor
    monitor --> telegram["Telegram alerts"]

    classDef source fill:#e0f2fe,stroke:#0284c7,color:#0f172a
    classDef service fill:#fef3c7,stroke:#d97706,color:#0f172a
    classDef output fill:#dcfce7,stroke:#16a34a,color:#0f172a

    class sub2api,state source
    class monitor service
    class telegram output
```

The daemon stores small local baselines in `STATE_FILE` so it can detect changes between polling intervals. Existing recharge totals are baselined on first run, so enabling the feature does not replay historical recharges.

## ✅ Requirements

- Linux host running Sub2API with PostgreSQL
- Docker CLI access to the Sub2API PostgreSQL container
- Python 3.11 or newer
- `git` and `rsync` for self-update support
- Telegram bot token and destination chat/channel ID

Default paths are conventional and configurable:

| Setting | Default |
| --- | --- |
| Sub2API directory | `/opt/sub2api` |
| Monitor install directory | `/opt/sub2api-monitor` |
| Monitor config | `/etc/sub2api-monitor/config.env` |
| Monitor state | `/var/lib/sub2api-monitor/state.json` |
| PostgreSQL container | `sub2api-postgres` |

## ⚡ Quick start

Download the management script and start the interactive installer:

```bash
curl -fsSL https://raw.githubusercontent.com/jiwen77/sub2api-monitor/main/monitor.sh -o /tmp/sub2api-monitor.sh
sudo bash /tmp/sub2api-monitor.sh
```

Recommended first-run flow:

1. Choose `安装/更新程序（从 GitHub 拉取）`
2. Choose `配置 Telegram 通知`
3. Choose `测试 Telegram 通知`
4. Choose `查看账号状态（只显示，不发 TG）`
5. Choose `发送账号状态到 TG（立即发送）`
6. Choose `后台启动/重启监控（推荐）`

After installation, reopen the menu with:

```bash
sudo /opt/sub2api-monitor/monitor.sh
```

## 🛠️ Configuration

The runtime config lives outside Git:

```text
/etc/sub2api-monitor/config.env
```

Start from [`config.env.example`](./config.env.example), then fill in local values on the server.

```env
TELEGRAM_BOT_TOKEN=<telegram-bot-token>
TELEGRAM_CHAT_ID=<telegram-chat-or-channel-id>
REDACT_IDENTIFIERS=true
USER_RECHARGE_ALERTS_ENABLED=true
POLL_INTERVAL_SECONDS=60
```

Important options:

| Option | Default | Description |
| --- | --- | --- |
| `SUB2API_DIR` | `/opt/sub2api` | Sub2API deployment directory. The monitor reads this directory's `.env` for PostgreSQL credentials when present |
| `POSTGRES_CONTAINER` | `sub2api-postgres` | PostgreSQL container name |
| `TELEGRAM_BOT_TOKEN` | empty | Telegram bot token; never commit a real value |
| `TELEGRAM_CHAT_ID` | empty | Telegram chat, group, channel, or user ID; never commit a real value |
| `TELEGRAM_ALLOWED_CHAT_IDS` | empty | Optional comma-separated allowlist for bot commands; defaults to `TELEGRAM_CHAT_ID` |
| `POLL_INTERVAL_SECONDS` | `60` | Daemon polling interval. Recharges detected within one interval are grouped into one notification |
| `REDACT_IDENTIFIERS` | `true` | Redact account and user names/emails in Telegram messages |
| `DETAIL_LIMIT` | `12` | Maximum rows expanded in one Telegram card |
| `USER_RECHARGE_ALERTS_ENABLED` | `true` | Send Telegram notifications when `users.total_recharged` increases |
| `ERROR_LOOKBACK_MINUTES` | `30` | Lookback window for new upstream errors |
| `ERROR_COOLDOWN_SECONDS` | `600` | Per-error-group cooldown to reduce repeated alerts |
| `UPSTREAM_ALLOWED_STATUS_CODES` | `429,500-599` | Provider/upstream HTTP statuses that should alert |
| `PROXY_ERROR_ALERTS_ENABLED` | `true` | Separately alert likely exit-proxy and network failures |
| `DAILY_REPORT_HOUR` | `0` | Local hour for daily report |
| `DAILY_REPORT_MINUTE` | `0` | Local minute for daily report |

Use menu option `13) 交互式修改配置项` for guided edits, or option `14) 手动编辑配置文件（nano）` for manual edits.

## 📣 Alerts and commands

### Alert categories

| Category | Source table | Trigger |
| --- | --- | --- |
| Account status | `accounts`, `account_groups`, `groups` | Account state or group-membership changes from the previous baseline |
| User recharge | `users` | `total_recharged` increases after the first baseline |
| Upstream error | `ops_error_logs` | Provider-side actionable status codes, usually `429` or `5xx` |
| Exit-network error | `ops_error_logs` | Proxy, DNS, timeout, TLS, connection reset/refused, or similar failures |
| Daily usage | `usage_logs` | Scheduled local-time daily summary |

### Telegram commands

Commands are accepted only from authorized chat IDs.

| Command | Description |
| --- | --- |
| `/status` | Send account overview and current non-normal account details, including group labels |
| `/accounts` | Send every account's current status, including group labels |
| `/daily` | Send the previous-day/current-day usage report |
| `/ping` | Check whether the daemon is receiving commands |
| `/help` | Show command help |

To authorize more than one chat, use placeholders like this:

```env
TELEGRAM_ALLOWED_CHAT_IDS=<chat-id-1>,<chat-id-2>
```

## 🧪 Manual commands

```bash
# Print the current account snapshot without sending Telegram.
python3 /opt/sub2api-monitor/sub2api_monitor.py --config /etc/sub2api-monitor/config.env account-summary

# Force-send the current account snapshot to Telegram.
python3 /opt/sub2api-monitor/sub2api_monitor.py --config /etc/sub2api-monitor/config.env account-summary --notify

# Run account, recharge, and upstream-error checks once.
python3 /opt/sub2api-monitor/sub2api_monitor.py --config /etc/sub2api-monitor/config.env run-once --notify

# Force-send the daily usage report.
python3 /opt/sub2api-monitor/sub2api_monitor.py --config /etc/sub2api-monitor/config.env daily --notify

# Register Telegram slash-command suggestions.
python3 /opt/sub2api-monitor/sub2api_monitor.py --config /etc/sub2api-monitor/config.env setup-telegram-commands

# Keep monitoring in the foreground.
python3 /opt/sub2api-monitor/sub2api_monitor.py --config /etc/sub2api-monitor/config.env daemon
```

## 🔁 Update and service management

Interactive update:

```bash
sudo /opt/sub2api-monitor/monitor.sh
# choose menu option 1
```

Non-interactive update:

```bash
sudo /opt/sub2api-monitor/monitor.sh --update
```

Systemd operations:

```bash
sudo systemctl enable --now sub2api-monitor.service
sudo systemctl status sub2api-monitor.service
sudo journalctl -u sub2api-monitor.service -f
```

For forks or pinned refs:

```bash
sudo UPDATE_REPO_URL=https://github.com/<owner>/<repo>.git \
  UPDATE_REF=main \
  /opt/sub2api-monitor/monitor.sh --update
```

## 🧑‍💻 Development

Run local checks before committing:

```bash
bash -n monitor.sh
python3 -m py_compile sub2api_monitor.py
PYTHONPATH=. python3 -m unittest discover -v -s tests
```

Recommended contribution checklist:

- Keep changes passive and read-only against Sub2API data
- Add or update tests for new alert logic
- Keep examples redacted and placeholder-based
- Do not commit runtime config, state files, logs, tokens, chat IDs, or database credentials

## 📄 License

MIT License. See [LICENSE](./LICENSE).
