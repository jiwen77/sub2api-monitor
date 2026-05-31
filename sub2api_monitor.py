#!/usr/bin/env python3
"""Sub2API passive monitor with Telegram notifications.

This monitor intentionally does NOT probe upstream APIs. It only reads Sub2API's
own PostgreSQL tables (accounts, ops_error_logs, usage_logs) and reports:
- account availability/state changes;
- provider/upstream errors already observed by Sub2API;
- daily usage summaries.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import logging
import os
import re
import subprocess
import sys
import textwrap
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

APP_NAME = "sub2api-monitor"
DEFAULT_CONFIG_PATHS = (
    "/etc/sub2api-monitor/config.env",
    "/opt/sub2api-monitor/config.env",
    "./config.env",
)
TOKEN_EXPR = (
    "coalesce(input_tokens,0)+coalesce(output_tokens,0)+"
    "coalesce(cache_creation_tokens,0)+coalesce(cache_read_tokens,0)+"
    "coalesce(cache_creation_5m_tokens,0)+coalesce(cache_creation_1h_tokens,0)+"
    "coalesce(image_output_tokens,0)"
)


@dataclass
class Config:
    config_path: str | None = None
    sub2api_dir: str = "/opt/sub2api"
    postgres_container: str = "sub2api-postgres"
    postgres_user: str = "sub2api"
    postgres_db: str = "sub2api"
    postgres_password: str = ""
    state_file: str = "/var/lib/sub2api-monitor/state.json"
    log_file: str = "/var/log/sub2api-monitor/monitor.log"
    timezone: str = "Asia/Shanghai"
    poll_interval_seconds: int = 60
    psql_timeout_seconds: int = 20
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_api_base: str = "https://api.telegram.org"
    telegram_disable_web_page_preview: bool = True
    telegram_parse_mode: str = "HTML"
    telegram_commands_enabled: bool = True
    telegram_command_poll_interval_seconds: int = 5
    telegram_allowed_chat_ids: str = ""
    telegram_drop_pending_updates: bool = True
    send_startup_summary: bool = True
    alert_existing_errors_on_first_run: bool = False
    redact_identifiers: bool = True
    detail_limit: int = 12
    error_lookback_minutes: int = 30
    error_limit_per_poll: int = 500
    error_cooldown_seconds: int = 600
    upstream_allowed_status_codes: str = "429,500-599"
    upstream_exclude_regex: str = (
        r"(?i)(api[\s_-]?key|user\s*key|invalid\s+key|unauthori[sz]ed|"
        r"forbidden|permission|authentication|invalidated\s+oauth\s+token|"
        r"network|timeout|timed\s*out|dns|connection\s+(?:reset|refused)|"
        r"client\s+(?:closed|disconnect|cancel)|failed\s+to\s+read\s+request\s+body)"
    )
    daily_report_hour: int = 0
    daily_report_minute: int = 0
    daily_catchup: bool = True

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        env: dict[str, str] = {}
        chosen = path or first_existing(DEFAULT_CONFIG_PATHS)
        if chosen:
            env.update(read_env_file(Path(chosen)))

        sub2api_dir = env.get("SUB2API_DIR") or os.environ.get("SUB2API_DIR") or cls.sub2api_dir
        # Sub2API's .env is the source of truth for Postgres credentials; explicit
        # monitor config/env overrides still win.
        sub_env_path = Path(sub2api_dir) / ".env"
        sub_env = read_env_file(sub_env_path) if sub_env_path.exists() else {}

        merged = {**sub_env, **env, **{k: v for k, v in os.environ.items() if k.startswith(("SUB2API_", "POSTGRES_", "TELEGRAM_"))}}
        cfg = cls()
        cfg.config_path = chosen
        cfg.sub2api_dir = get_str(merged, "SUB2API_DIR", cfg.sub2api_dir)
        cfg.postgres_container = get_str(merged, "POSTGRES_CONTAINER", cfg.postgres_container)
        cfg.postgres_user = get_str(merged, "POSTGRES_USER", cfg.postgres_user)
        cfg.postgres_db = get_str(merged, "POSTGRES_DB", cfg.postgres_db)
        cfg.postgres_password = get_str(merged, "POSTGRES_PASSWORD", cfg.postgres_password)
        cfg.state_file = get_str(merged, "STATE_FILE", cfg.state_file)
        cfg.log_file = get_str(merged, "LOG_FILE", cfg.log_file)
        cfg.timezone = get_str(merged, "TZ", get_str(merged, "TIMEZONE", cfg.timezone))
        cfg.poll_interval_seconds = get_int(merged, "POLL_INTERVAL_SECONDS", cfg.poll_interval_seconds, 5, 3600)
        cfg.psql_timeout_seconds = get_int(merged, "PSQL_TIMEOUT_SECONDS", cfg.psql_timeout_seconds, 3, 120)
        cfg.telegram_bot_token = get_str(merged, "TELEGRAM_BOT_TOKEN", cfg.telegram_bot_token)
        cfg.telegram_chat_id = get_str(merged, "TELEGRAM_CHAT_ID", cfg.telegram_chat_id)
        cfg.telegram_api_base = get_str(merged, "TELEGRAM_API_BASE", cfg.telegram_api_base).rstrip("/")
        cfg.telegram_disable_web_page_preview = get_bool(merged, "TELEGRAM_DISABLE_WEB_PAGE_PREVIEW", cfg.telegram_disable_web_page_preview)
        cfg.telegram_parse_mode = get_str(merged, "TELEGRAM_PARSE_MODE", cfg.telegram_parse_mode).strip()
        cfg.telegram_commands_enabled = get_bool(merged, "TELEGRAM_COMMANDS_ENABLED", cfg.telegram_commands_enabled)
        cfg.telegram_command_poll_interval_seconds = get_int(merged, "TELEGRAM_COMMAND_POLL_INTERVAL_SECONDS", cfg.telegram_command_poll_interval_seconds, 1, 300)
        cfg.telegram_allowed_chat_ids = get_str(merged, "TELEGRAM_ALLOWED_CHAT_IDS", cfg.telegram_allowed_chat_ids).strip()
        cfg.telegram_drop_pending_updates = get_bool(merged, "TELEGRAM_DROP_PENDING_UPDATES", cfg.telegram_drop_pending_updates)
        cfg.send_startup_summary = get_bool(merged, "SEND_STARTUP_SUMMARY", cfg.send_startup_summary)
        cfg.alert_existing_errors_on_first_run = get_bool(merged, "ALERT_EXISTING_ERRORS_ON_FIRST_RUN", cfg.alert_existing_errors_on_first_run)
        cfg.redact_identifiers = get_bool(merged, "REDACT_IDENTIFIERS", cfg.redact_identifiers)
        cfg.detail_limit = get_int(merged, "DETAIL_LIMIT", cfg.detail_limit, 1, 100)
        cfg.error_lookback_minutes = get_int(merged, "ERROR_LOOKBACK_MINUTES", cfg.error_lookback_minutes, 1, 1440)
        cfg.error_limit_per_poll = get_int(merged, "ERROR_LIMIT_PER_POLL", cfg.error_limit_per_poll, 1, 5000)
        cfg.error_cooldown_seconds = get_int(merged, "ERROR_COOLDOWN_SECONDS", cfg.error_cooldown_seconds, 0, 86400)
        cfg.upstream_allowed_status_codes = get_str(merged, "UPSTREAM_ALLOWED_STATUS_CODES", cfg.upstream_allowed_status_codes)
        cfg.upstream_exclude_regex = get_str(merged, "UPSTREAM_EXCLUDE_REGEX", cfg.upstream_exclude_regex)
        cfg.daily_report_hour = get_int(merged, "DAILY_REPORT_HOUR", cfg.daily_report_hour, 0, 23)
        cfg.daily_report_minute = get_int(merged, "DAILY_REPORT_MINUTE", cfg.daily_report_minute, 0, 59)
        cfg.daily_catchup = get_bool(merged, "DAILY_CATCHUP", cfg.daily_catchup)
        return cfg

    def tzinfo(self) -> dt.tzinfo:
        if ZoneInfo is None:
            return dt.timezone.utc
        try:
            return ZoneInfo(self.timezone)
        except Exception:
            logging.warning("invalid TZ=%s; falling back to UTC", self.timezone)
            return dt.timezone.utc


@dataclass
class MonitorResult:
    account_message: str | None = None
    error_message: str | None = None
    daily_message: str | None = None
    changed: bool = False


@dataclass
class Monitor:
    cfg: Config
    dry_run: bool = False
    state: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.state = load_state(Path(self.cfg.state_file))
        self.state.setdefault("version", 1)
        self.state.setdefault("accounts", {})
        self.state.setdefault("last_error_id", 0)
        self.state.setdefault("error_cooldowns", {})
        self.allowed_status = parse_allowed_status(self.cfg.upstream_allowed_status_codes)
        self.exclude_re = re.compile(self.cfg.upstream_exclude_regex) if self.cfg.upstream_exclude_regex else None

    def save(self) -> None:
        save_state(Path(self.cfg.state_file), self.state)

    def run_once(self, notify: bool = False, include_daily: bool = True) -> MonitorResult:
        result = MonitorResult()
        try:
            account_msg = self.check_accounts()
            if account_msg:
                result.account_message = account_msg
                result.changed = True
                if notify:
                    self.send(account_msg)
        except Exception:
            logging.exception("account check failed")
            msg = build_simple_alert("⚠️ 账号状态检查失败", short_exc(), self.cfg)
            if notify:
                self.send(msg)

        try:
            error_msg = self.check_upstream_errors()
            if error_msg:
                result.error_message = error_msg
                result.changed = True
                if notify:
                    self.send(error_msg)
        except Exception:
            logging.exception("upstream error check failed")
            msg = build_simple_alert("⚠️ 上游错误检查失败", short_exc(), self.cfg)
            if notify:
                self.send(msg)

        if include_daily:
            try:
                daily_msg = self.maybe_daily_report()
                if daily_msg:
                    result.daily_message = daily_msg
                    if notify:
                        self.send(daily_msg)
            except Exception:
                logging.exception("daily report failed")
                msg = build_simple_alert("⚠️ 日报生成失败", short_exc(), self.cfg)
                if notify:
                    self.send(msg)

        self.save()
        return result

    def daemon(self) -> None:
        logging.info(
            "daemon started, poll=%ss, tg_commands=%s, config=%s",
            self.cfg.poll_interval_seconds,
            self.telegram_commands_active(),
            self.cfg.config_path,
        )
        # Establish a baseline immediately; notify only when configured.
        self.run_once(notify=True, include_daily=True)
        self.bootstrap_telegram_commands()

        next_monitor_at = time.monotonic() + self.cfg.poll_interval_seconds
        while True:
            if self.telegram_commands_active():
                try:
                    self.poll_telegram_commands()
                except Exception:
                    logging.exception("telegram command polling failed")

            now = time.monotonic()
            if now >= next_monitor_at:
                self.run_once(notify=True, include_daily=True)
                next_monitor_at = time.monotonic() + self.cfg.poll_interval_seconds

            if self.telegram_commands_active():
                sleep_for = min(
                    self.cfg.telegram_command_poll_interval_seconds,
                    max(1.0, next_monitor_at - time.monotonic()),
                )
            else:
                sleep_for = max(1.0, next_monitor_at - time.monotonic())
            time.sleep(sleep_for)

    def telegram_commands_active(self) -> bool:
        return bool(self.cfg.telegram_commands_enabled and self.cfg.telegram_bot_token)

    def bootstrap_telegram_commands(self) -> None:
        if not self.telegram_commands_active():
            return
        if self.state.get("telegram_update_offset") is not None:
            return
        if not self.cfg.telegram_drop_pending_updates:
            return
        try:
            updates = telegram_get_updates(self.cfg, offset=None, timeout_seconds=0)
        except Exception:
            logging.exception("failed to initialize telegram update offset")
            return
        if updates:
            max_update_id = max(int(update.get("update_id") or 0) for update in updates)
            self.state["telegram_update_offset"] = max_update_id + 1
            self.save()
            logging.info("dropped %s pending telegram updates during bootstrap", len(updates))
        else:
            self.state["telegram_update_offset"] = 0
            self.save()

    def poll_telegram_commands(self) -> None:
        offset = self.state.get("telegram_update_offset")
        try:
            offset_int = int(offset) if offset is not None else None
        except Exception:
            offset_int = None
        updates = telegram_get_updates(self.cfg, offset=offset_int, timeout_seconds=0)
        if not updates:
            return
        max_update_id = offset_int or 0
        for update in updates:
            update_id = int(update.get("update_id") or 0)
            max_update_id = max(max_update_id, update_id)
            self.handle_telegram_update(update)
        self.state["telegram_update_offset"] = max_update_id + 1
        self.save()

    def handle_telegram_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        if not isinstance(chat, dict):
            return
        chat_id = str(chat.get("id") or "").strip()
        text = str(message.get("text") or "").strip()
        if not chat_id or not text.startswith("/"):
            return
        if not telegram_chat_allowed(self.cfg, chat_id):
            logging.warning("ignored telegram command from unauthorized chat_id=%s", chat_id)
            return

        command = normalize_telegram_command(text)
        logging.info("telegram command chat_id=%s command=%s", chat_id, command)
        try:
            if command in {"/start", "/help"}:
                self.send(build_command_help(), chat_id=chat_id)
            elif command in {"/status", "/accounts", "/account", "/summary"}:
                rows = self.current_account_rows()
                self.send(build_account_message(rows, [], [], [], self.cfg, title="ℹ️ sub2api 当前账号状态"), chat_id=chat_id)
            elif command in {"/daily", "/report"}:
                tz = self.cfg.tzinfo()
                day = dt.datetime.now(tz).date() - dt.timedelta(days=1)
                self.send(self.build_daily_report(day), chat_id=chat_id)
            elif command == "/ping":
                self.send("🏓 <b>pong</b>\n" + muted(now_iso(self.cfg.tzinfo())), chat_id=chat_id)
            else:
                self.send(build_unknown_command(command), chat_id=chat_id)
        except Exception:
            logging.exception("telegram command handling failed: %s", command)
            self.send(build_simple_alert("⚠️ Telegram 命令执行失败", short_exc(), self.cfg), chat_id=chat_id)

    def query_json(self, sql: str) -> Any:
        raw = self.psql(sql)
        text = raw.strip()
        if not text:
            return None
        return json.loads(text)

    def psql(self, sql: str) -> str:
        env_arg = f"PGPASSWORD={self.cfg.postgres_password}"
        cmd = [
            "docker",
            "exec",
            "-i",
            "-e",
            env_arg,
            self.cfg.postgres_container,
            "psql",
            "-U",
            self.cfg.postgres_user,
            "-d",
            self.cfg.postgres_db,
            "-At",
            "-P",
            "pager=off",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        ]
        logging.debug("running psql query against %s/%s", self.cfg.postgres_container, self.cfg.postgres_db)
        proc = subprocess.run(
            cmd,
            cwd=self.cfg.sub2api_dir if Path(self.cfg.sub2api_dir).exists() else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.cfg.psql_timeout_seconds,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"psql failed rc={proc.returncode}: {proc.stderr.strip()[:500]}")
        return proc.stdout

    def current_account_rows(self) -> list[dict[str, Any]]:
        sql = """
WITH rows AS (
  SELECT
    id,
    platform,
    type,
    coalesce(nullif(credentials->>'plan_type',''), nullif(extra->>'plan_type',''), type, 'unknown') AS plan,
    status,
    schedulable,
    (rate_limit_reset_at IS NOT NULL AND rate_limit_reset_at > now()) AS rate_limited,
    rate_limit_reset_at,
    (overload_until IS NOT NULL AND overload_until > now()) AS overloaded,
    overload_until,
    (temp_unschedulable_until IS NOT NULL AND temp_unschedulable_until > now()) AS temp_unschedulable,
    temp_unschedulable_until,
    coalesce(temp_unschedulable_reason, '') AS temp_unschedulable_reason,
    (auto_pause_on_expired AND expires_at IS NOT NULL AND expires_at <= now()) AS expired,
    expires_at,
    coalesce(error_message, '') AS error_message,
    last_used_at,
    updated_at,
    name,
    coalesce(credentials->>'email', extra->>'email', '') AS email,
    CASE WHEN (extra->>'codex_5h_used_percent') ~ '^-?[0-9]+(\\.[0-9]+)?$'
      THEN round((extra->>'codex_5h_used_percent')::numeric, 2) ELSE NULL END AS codex_5h_used_percent,
    CASE WHEN (extra->>'codex_7d_used_percent') ~ '^-?[0-9]+(\\.[0-9]+)?$'
      THEN round((extra->>'codex_7d_used_percent')::numeric, 2) ELSE NULL END AS codex_7d_used_percent,
    CASE
      WHEN status = 'active'
       AND schedulable IS TRUE
       AND NOT (rate_limit_reset_at IS NOT NULL AND rate_limit_reset_at > now())
       AND NOT (overload_until IS NOT NULL AND overload_until > now())
       AND NOT (temp_unschedulable_until IS NOT NULL AND temp_unschedulable_until > now())
       AND NOT (auto_pause_on_expired AND expires_at IS NOT NULL AND expires_at <= now())
      THEN true ELSE false
    END AS normal
  FROM accounts
  WHERE deleted_at IS NULL
  ORDER BY platform, plan, id
)
SELECT coalesce(json_agg(row_to_json(rows)), '[]'::json)::text FROM rows;
"""
        data = self.query_json(sql)
        return data or []

    def check_accounts(self) -> str | None:
        rows = self.current_account_rows()
        now = now_iso(self.cfg.tzinfo())
        prev: dict[str, Any] = self.state.get("accounts") or {}
        current = {str(r["id"]): account_digest(r) for r in rows}
        first_run = not bool(prev)
        self.state["accounts"] = current
        self.state["accounts_updated_at"] = now

        if first_run:
            # On the first run we set an error baseline unless explicitly asked to
            # alert historical errors. This prevents a Telegram storm when enabling
            # the daemon on an existing busy Sub2API instance.
            if not self.cfg.alert_existing_errors_on_first_run:
                self.state["last_error_id"] = self.max_ops_error_id()
            if self.cfg.send_startup_summary:
                return build_account_message(rows, [], [], [], self.cfg, title="✅ sub2api 监控启动基线")
            return None

        added = [rows_by_id(rows)[aid] for aid in current.keys() - prev.keys()]
        removed = [prev[aid] for aid in prev.keys() - current.keys()]
        changed: list[dict[str, Any]] = []
        by_id = rows_by_id(rows)
        for aid in sorted(current.keys() & prev.keys(), key=lambda x: int(x) if x.isdigit() else x):
            if current[aid] != prev[aid]:
                row = dict(by_id[aid])
                row["_previous"] = prev[aid]
                changed.append(row)
        if not added and not removed and not changed:
            return None
        return build_account_message(rows, changed, added, removed, self.cfg, title="🔔 sub2api 账号状态变更")

    def max_ops_error_id(self) -> int:
        sql = "SELECT coalesce(max(id),0)::bigint FROM ops_error_logs;"
        out = self.psql(sql).strip()
        try:
            return int(out or "0")
        except ValueError:
            return 0

    def fetch_new_error_rows(self) -> list[dict[str, Any]]:
        last_id = int(self.state.get("last_error_id") or 0)
        lookback = max(1, min(int(self.cfg.error_lookback_minutes), 1440))
        limit = max(1, min(int(self.cfg.error_limit_per_poll), 5000))
        sql = f"""
WITH rows AS (
  SELECT
    id,
    created_at,
    platform,
    coalesce(nullif(requested_model,''), nullif(upstream_model,''), nullif(model,''), '') AS model,
    status_code,
    upstream_status_code,
    coalesce(error_type, '') AS error_type,
    coalesce(provider_error_type, '') AS provider_error_type,
    coalesce(provider_error_code, '') AS provider_error_code,
    coalesce(network_error_type, '') AS network_error_type,
    coalesce(error_owner, '') AS error_owner,
    coalesce(error_source, '') AS error_source,
    coalesce(is_business_limited, false) AS is_business_limited,
    coalesce(nullif(upstream_error_message,''), nullif(error_message,''), nullif(provider_error_type,''), '') AS message,
    account_id,
    upstream_endpoint
  FROM ops_error_logs
  WHERE id > {last_id}
    AND created_at >= now() - interval '{lookback} minutes'
  ORDER BY id ASC
  LIMIT {limit}
)
SELECT coalesce(json_agg(row_to_json(rows)), '[]'::json)::text FROM rows;
"""
        return self.query_json(sql) or []

    def is_actionable_upstream_error(self, row: dict[str, Any]) -> bool:
        if row.get("error_owner") != "provider":
            return False
        if not str(row.get("error_source") or "").startswith("upstream"):
            return False
        if row.get("network_error_type"):
            return False
        if row.get("is_business_limited") is True:
            return False
        status = row.get("upstream_status_code") or row.get("status_code")
        if not status_allowed(status, self.allowed_status):
            return False
        message = str(row.get("message") or "")
        if self.exclude_re and self.exclude_re.search(message):
            return False
        return True

    def check_upstream_errors(self) -> str | None:
        rows = self.fetch_new_error_rows()
        if not rows:
            return None
        max_id = max(int(r.get("id") or 0) for r in rows)
        actionable = [r for r in rows if self.is_actionable_upstream_error(r)]
        self.state["last_error_id"] = max(max_id, int(self.state.get("last_error_id") or 0))
        if not actionable:
            return None

        cooldowns: dict[str, Any] = self.state.setdefault("error_cooldowns", {})
        now_ts = int(time.time())
        # prune stale cooldown keys
        for key in list(cooldowns.keys()):
            try:
                if int(cooldowns[key]) <= now_ts:
                    cooldowns.pop(key, None)
            except Exception:
                cooldowns.pop(key, None)

        grouped: dict[str, dict[str, Any]] = {}
        suppressed = 0
        for row in actionable:
            key = error_key(row)
            if key not in grouped:
                if self.cfg.error_cooldown_seconds > 0 and key in cooldowns:
                    suppressed += 1
                    continue
                if self.cfg.error_cooldown_seconds > 0:
                    cooldowns[key] = now_ts + self.cfg.error_cooldown_seconds
            g = grouped.setdefault(key, {"count": 0, "rows": [], "sample": row})
            g["count"] += 1
            g["rows"].append(row)

        if not grouped:
            logging.info("upstream errors suppressed by cooldown: %s", suppressed)
            return None
        return build_error_message(grouped, suppressed, self.cfg)

    def maybe_daily_report(self) -> str | None:
        tz = self.cfg.tzinfo()
        now = dt.datetime.now(tz)
        scheduled = now.replace(hour=self.cfg.daily_report_hour, minute=self.cfg.daily_report_minute, second=0, microsecond=0)
        if now < scheduled:
            return None
        report_key = now.date().isoformat()  # The report sent on this local date.
        if self.state.get("daily_report_sent_for") == report_key:
            return None
        if not self.cfg.daily_catchup and (now - scheduled).total_seconds() > self.cfg.poll_interval_seconds * 2 + 30:
            return None
        previous_day = now.date() - dt.timedelta(days=1)
        message = self.build_daily_report(previous_day, now)
        self.state["daily_report_sent_for"] = report_key
        self.state["daily_report_sent_at"] = now.isoformat()
        return message

    def build_daily_report(self, day: dt.date, now: dt.datetime | None = None) -> str:
        tz = self.cfg.tzinfo()
        now = now or dt.datetime.now(tz)
        yesterday = self.usage_stats_for_day(day)
        today = self.usage_stats_for_day(now.date(), until_now=True)
        return build_daily_message(day, yesterday, now.date(), today, self.cfg, now)

    def usage_stats_for_day(self, day: dt.date, until_now: bool = False) -> dict[str, Any]:
        tz_name = sql_literal(self.cfg.timezone)
        start = f"{day.isoformat()} 00:00:00"
        next_day = (day + dt.timedelta(days=1)).isoformat() + " 00:00:00"
        end_expr = "now()" if until_now else f"('{next_day}'::timestamp AT TIME ZONE {tz_name})"
        start_expr = f"('{start}'::timestamp AT TIME ZONE {tz_name})"
        token_expr = TOKEN_EXPR
        sql = f"""
WITH base AS (
  SELECT
    u.*,
    coalesce(nullif(u.requested_model,''), nullif(u.upstream_model,''), nullif(u.model,''), '(unknown)') AS display_model,
    coalesce(a.platform, 'unknown') AS account_platform,
    coalesce(nullif(a.credentials->>'plan_type',''), nullif(a.extra->>'plan_type',''), a.type, 'unknown') AS account_plan,
    ({token_expr})::bigint AS total_tokens_calc,
    (coalesce(u.cache_creation_tokens,0)+coalesce(u.cache_read_tokens,0)+coalesce(u.cache_creation_5m_tokens,0)+coalesce(u.cache_creation_1h_tokens,0))::bigint AS cache_tokens_calc
  FROM usage_logs u
  LEFT JOIN accounts a ON a.id = u.account_id
  WHERE u.created_at >= {start_expr}
    AND u.created_at < {end_expr}
), summary AS (
  SELECT
    count(*)::bigint AS requests,
    coalesce(sum(input_tokens),0)::bigint AS input_tokens,
    coalesce(sum(output_tokens),0)::bigint AS output_tokens,
    coalesce(sum(cache_tokens_calc),0)::bigint AS cache_tokens,
    coalesce(sum(image_output_tokens),0)::bigint AS image_output_tokens,
    coalesce(sum(total_tokens_calc),0)::bigint AS total_tokens,
    coalesce(sum(total_cost),0)::numeric(20,6) AS total_cost,
    coalesce(sum(actual_cost),0)::numeric(20,6) AS actual_cost,
    round(coalesce(avg(duration_ms),0)::numeric, 1) AS avg_duration_ms,
    round(coalesce(avg(first_token_ms),0)::numeric, 1) AS avg_first_token_ms
  FROM base
), by_plan AS (
  SELECT account_platform AS platform, account_plan AS plan,
         count(*)::bigint AS requests,
         coalesce(sum(total_tokens_calc),0)::bigint AS total_tokens,
         coalesce(sum(total_cost),0)::numeric(20,6) AS total_cost
  FROM base
  GROUP BY 1,2
  ORDER BY total_tokens DESC, requests DESC
  LIMIT 20
), top_models AS (
  SELECT display_model AS model,
         count(*)::bigint AS requests,
         coalesce(sum(total_tokens_calc),0)::bigint AS total_tokens,
         coalesce(sum(total_cost),0)::numeric(20,6) AS total_cost
  FROM base
  GROUP BY 1
  ORDER BY total_tokens DESC, requests DESC
  LIMIT 10
)
SELECT json_build_object(
  'summary', (SELECT row_to_json(summary) FROM summary),
  'by_plan', coalesce((SELECT json_agg(row_to_json(by_plan)) FROM by_plan), '[]'::json),
  'top_models', coalesce((SELECT json_agg(row_to_json(top_models)) FROM top_models), '[]'::json)
)::text;
"""
        return self.query_json(sql) or {"summary": {}, "by_plan": [], "top_models": []}

    def send(self, text: str, chat_id: str | None = None) -> None:
        send_telegram(self.cfg, text, dry_run=self.dry_run, chat_id=chat_id)


# ----------------------------- formatting helpers -----------------------------


def account_digest(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "platform": row.get("platform"),
        "type": row.get("type"),
        "plan": row.get("plan"),
        "status": row.get("status"),
        "normal": bool(row.get("normal")),
        "schedulable": bool(row.get("schedulable")),
        "rate_limited": bool(row.get("rate_limited")),
        "rate_limit_reset_at": row.get("rate_limit_reset_at"),
        "overloaded": bool(row.get("overloaded")),
        "overload_until": row.get("overload_until"),
        "temp_unschedulable": bool(row.get("temp_unschedulable")),
        "temp_unschedulable_until": row.get("temp_unschedulable_until"),
        "temp_unschedulable_reason": truncate(str(row.get("temp_unschedulable_reason") or ""), 140),
        "expired": bool(row.get("expired")),
        "expires_at": row.get("expires_at"),
        "error_message_hash": stable_hash(str(row.get("error_message") or "")) if row.get("error_message") else "",
    }


def rows_by_id(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(r.get("id")): r for r in rows}


def summarize_accounts(rows: list[dict[str, Any]]) -> tuple[list[str], dict[tuple[str, str], dict[str, int]]]:
    buckets: dict[tuple[str, str], dict[str, int]] = {}
    for row in rows:
        key = (str(row.get("platform") or "unknown"), str(row.get("plan") or "unknown"))
        b = buckets.setdefault(key, {"normal": 0, "total": 0, "rate_limited": 0, "error": 0, "overloaded": 0, "temp": 0, "expired": 0})
        b["total"] += 1
        if row.get("normal"):
            b["normal"] += 1
        if row.get("rate_limited"):
            b["rate_limited"] += 1
        if row.get("status") not in ("active", ""):
            b["error"] += 1
        if row.get("overloaded"):
            b["overloaded"] += 1
        if row.get("temp_unschedulable"):
            b["temp"] += 1
        if row.get("expired"):
            b["expired"] += 1
    lines: list[str] = []
    for key, b in sorted(buckets.items()):
        lines.append(format_summary_line(key, b, html_mode=False))
    return lines, buckets


def build_account_message(
    rows: list[dict[str, Any]],
    changed: list[dict[str, Any]],
    added: list[dict[str, Any]],
    removed: list[dict[str, Any]],
    cfg: Config,
    title: str,
) -> str:
    _summary_lines, buckets = summarize_accounts(rows)
    lines = [
        f"{h(title)}",
        muted(now_iso(cfg.tzinfo())),
        "",
        section("账号概览"),
    ]
    for key, bucket in sorted(buckets.items()):
        lines.append(format_summary_line(key, bucket, html_mode=True))

    abnormal = sorted([r for r in rows if not r.get("normal")], key=account_sort_key)
    if changed or added or removed:
        lines += ["", section(f"变化 · {len(changed)} 变更 / {len(added)} 新增 / {len(removed)} 移除")]
        shown = 0
        for label, icon, items in (("变更", "🔄", changed), ("新增", "➕", added)):
            for row in items[: cfg.detail_limit]:
                lines.append(format_account_row(row, cfg, prefix=f"{icon} {label}"))
                shown += 1
        for old in removed[: cfg.detail_limit]:
            lines.append(
                f"➖ 移除 {tg_code('#' + str(old.get('id')))} "
                f"{tg_code(str(old.get('platform') or 'unknown') + '/' + str(old.get('plan') or 'unknown'))}"
            )
            shown += 1
        total_changes = len(changed) + len(added) + len(removed)
        if total_changes > shown:
            lines.append(muted(f"另有 {total_changes - shown} 条变化未展开"))

    if abnormal:
        lines += ["", section("需要关注")]
        for row in abnormal[: cfg.detail_limit]:
            lines.append(format_account_row(row, cfg))
        if len(abnormal) > cfg.detail_limit:
            lines.append(muted(f"另有 {len(abnormal) - cfg.detail_limit} 个非正常账号未展开"))
    return clamp_message("\n".join(lines))


def account_sort_key(row: dict[str, Any]) -> tuple[int, int]:
    if row.get("status") not in ("active", ""):
        severity = 0
    elif row.get("rate_limited"):
        severity = 1
    else:
        severity = 2
    try:
        account_id = int(row.get("id") or 0)
    except Exception:
        account_id = 0
    return severity, account_id


def format_summary_line(key: tuple[str, str], bucket: dict[str, int], html_mode: bool) -> str:
    platform, plan = key
    total = max(int(bucket.get("total") or 0), 0)
    normal = max(int(bucket.get("normal") or 0), 0)
    extras = []
    if bucket.get("rate_limited"):
        extras.append(f"限流{bucket['rate_limited']}")
    if bucket.get("error"):
        extras.append(f"异常{bucket['error']}")
    if bucket.get("overloaded"):
        extras.append(f"过载{bucket['overloaded']}")
    if bucket.get("temp"):
        extras.append(f"临停{bucket['temp']}")
    if bucket.get("expired"):
        extras.append(f"过期{bucket['expired']}")
    icon = summary_icon(normal, total, extras)
    label = f"{platform}/{plan}"
    if html_mode:
        base = f"{icon} {tg_code(label)} {normal}/{total}"
        return base + (f" · {h(' · '.join(extras))}" if extras else "")
    return f"{icon} {label} {normal}/{total}" + (f" · {' · '.join(extras)}" if extras else "")


def summary_icon(normal: int, total: int, extras: list[str]) -> str:
    if total <= 0 or normal == 0:
        return "🔴"
    if normal < total or extras:
        return "🟡"
    return "🟢"


def format_account_row(row: dict[str, Any], cfg: Config, prefix: str = "") -> str:
    icon = account_icon(row)
    label = f"{row.get('platform') or 'unknown'}/{row.get('plan') or 'unknown'}"
    ident = tg_code(f"#{row.get('id')}")
    name = display_identifier(row, cfg)
    head_bits = [icon]
    if prefix:
        head_bits.append(h(prefix))
    head_bits.append(ident)
    if name:
        head_bits.append(h(name))
    head_bits.append(tg_code(label))

    detail = account_state_summary(row)
    quota = account_quota_summary(row)
    second_bits = [detail]
    if quota:
        second_bits.append(quota)
    return " ".join(head_bits) + "\n  " + h(" · ").join(second_bits)


def account_icon(row: dict[str, Any]) -> str:
    if row.get("status") not in ("active", ""):
        return "🔴"
    if row.get("rate_limited"):
        return "🟡"
    if row.get("overloaded") or row.get("temp_unschedulable") or row.get("expired"):
        return "🟠"
    if not row.get("normal"):
        return "⚪️"
    return "🟢"


def account_state_summary(row: dict[str, Any]) -> str:
    if row.get("status") not in ("active", ""):
        err = clean_account_error(str(row.get("error_message") or ""))
        return h(f"异常：{row.get('status') or 'unknown'}" + (f" · {err}" if err else ""))
    if row.get("rate_limited"):
        return h(f"限流至 {short_time(row.get('rate_limit_reset_at'))}")
    if row.get("overloaded"):
        return h(f"过载至 {short_time(row.get('overload_until'))}")
    if row.get("temp_unschedulable"):
        reason = clean_account_error(str(row.get("temp_unschedulable_reason") or ""))
        return h(f"临停至 {short_time(row.get('temp_unschedulable_until'))}" + (f" · {reason}" if reason else ""))
    if row.get("expired"):
        return h(f"已过期 {short_time(row.get('expires_at'))}")
    if not row.get("schedulable"):
        return h("不可调度")
    if row.get("normal"):
        return h("正常")
    return h("非正常")


def account_quota_summary(row: dict[str, Any]) -> str:
    parts = []
    if row.get("codex_5h_used_percent") is not None:
        parts.append(f"5h {row.get('codex_5h_used_percent')}%")
    if row.get("codex_7d_used_percent") is not None:
        parts.append(f"7d {row.get('codex_7d_used_percent')}%")
    return h(" · ").join(h(part) for part in parts)


def clean_account_error(message: str) -> str:
    message = re.sub(r"\s+", " ", message).strip()
    replacements = [
        (r"^Token revoked \(401\):.*", "Token revoked (401)"),
        (r"Encountered invalidated oauth token.*", "Invalidated OAuth token"),
    ]
    for pattern, repl in replacements:
        if re.search(pattern, message, flags=re.I):
            return repl
    return truncate(message, 64)


def display_identifier(row: dict[str, Any], cfg: Config) -> str:
    raw = str(row.get("email") or row.get("name") or "").strip()
    if not raw:
        return ""
    if cfg.redact_identifiers:
        if "@" in raw:
            left, _, domain = raw.partition("@")
            return f"{left[:2]}***@{domain[:2]}***"
        if len(raw) <= 4:
            return "***"
        return f"{raw[:2]}***{raw[-2:]}"
    return raw


def build_error_message(grouped: dict[str, dict[str, Any]], suppressed: int, cfg: Config) -> str:
    total = sum(int(g["count"]) for g in grouped.values())
    lines = [
        "🚨 <b>Sub2API 上游错误</b>",
        muted(now_iso(cfg.tzinfo())),
        h(f"新增 {total} 条 / {len(grouped)} 组") + (h(f" · 冷却抑制 {suppressed} 条") if suppressed else ""),
        "",
        section("错误分组"),
    ]
    for group in sorted(grouped.values(), key=lambda g: -int(g["count"]))[: cfg.detail_limit]:
        sample = group["sample"]
        status = sample.get("upstream_status_code") or sample.get("status_code") or "?"
        model = sample.get("model") or "unknown-model"
        msg = truncate(re.sub(r"\s+", " ", str(sample.get("message") or "")).strip(), 120)
        ids = ",".join(str(r.get("id")) for r in group.get("rows", [])[:5])
        lines.append(
            f"🔴 {tg_code(str(sample.get('platform') or 'unknown') + '/' + str(model))} "
            f"{tg_code(str(status))} ×{int(group['count'])}"
        )
        lines.append(f"  {h(msg)}" + (f" · {tg_code('ids ' + ids)}" if ids else ""))
    if len(grouped) > cfg.detail_limit:
        lines.append(muted(f"另有 {len(grouped) - cfg.detail_limit} 组未展开"))
    return clamp_message("\n".join(lines))


def build_daily_message(day: dt.date, yesterday: dict[str, Any], today_date: dt.date, today: dict[str, Any], cfg: Config, now: dt.datetime) -> str:
    ys = yesterday.get("summary") or {}
    ts = today.get("summary") or {}
    lines = [
        "📊 <b>Sub2API 每日用量</b>",
        muted(now.strftime('%Y-%m-%d %H:%M:%S %Z')),
        "",
        section(f"昨日 {day.isoformat()}"),
        f"Tokens {tg_code(fmt_compact_int(ys.get('total_tokens')))} · Requests {tg_code(fmt_int(ys.get('requests')))} · Cost {tg_code(fmt_money(ys.get('total_cost')))}",
        f"Input {tg_code(fmt_compact_int(ys.get('input_tokens')))} · Output {tg_code(fmt_compact_int(ys.get('output_tokens')))} · Cache {tg_code(fmt_compact_int(ys.get('cache_tokens')))}",
        f"Avg {tg_code(str(ys.get('avg_duration_ms', 0)) + ' ms')} · First token {tg_code(str(ys.get('avg_first_token_ms', 0)) + ' ms')}",
        "",
        section(f"今日 {today_date.isoformat()} 截至当前"),
        f"Tokens {tg_code(fmt_compact_int(ts.get('total_tokens')))} · Requests {tg_code(fmt_int(ts.get('requests')))} · Cost {tg_code(fmt_money(ts.get('total_cost')))}",
    ]
    if yesterday.get("by_plan"):
        lines += ["", section("昨日按账号类型")]
        for row in yesterday["by_plan"][: cfg.detail_limit]:
            lines.append(
                f"• {tg_code(str(row.get('platform') or 'unknown') + '/' + str(row.get('plan') or 'unknown'))} "
                f"{h(fmt_compact_int(row.get('total_tokens')) + ' tokens')} · {h(fmt_int(row.get('requests')) + ' req')}"
            )
    if yesterday.get("top_models"):
        lines += ["", section("昨日 Top 模型")]
        for row in yesterday["top_models"][: min(8, cfg.detail_limit)]:
            lines.append(
                f"• {tg_code(str(row.get('model') or 'unknown'))} "
                f"{h(fmt_compact_int(row.get('total_tokens')) + ' tokens')} · {h(fmt_int(row.get('requests')) + ' req')}"
            )
    return clamp_message("\n".join(lines))


def build_simple_alert(title: str, detail: str, cfg: Config) -> str:
    return "\n".join([h(title), muted(now_iso(cfg.tzinfo())), h(truncate(detail, 500))])


# ------------------------------- predicates ----------------------------------


def parse_allowed_status(spec: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                ranges.append((int(a), int(b)))
            except ValueError:
                continue
        else:
            try:
                n = int(part)
                ranges.append((n, n))
            except ValueError:
                continue
    return ranges or [(429, 429), (500, 599)]


def status_allowed(status: Any, ranges: list[tuple[int, int]]) -> bool:
    try:
        n = int(status)
    except Exception:
        return False
    return any(a <= n <= b for a, b in ranges)


def error_key(row: dict[str, Any]) -> str:
    msg = re.sub(r"\s+", " ", str(row.get("message") or "")).strip().lower()[:160]
    raw = "|".join(
        str(x or "")
        for x in (
            row.get("platform"),
            row.get("model"),
            row.get("upstream_status_code") or row.get("status_code"),
            row.get("error_type"),
            row.get("provider_error_type"),
            row.get("provider_error_code"),
            msg,
        )
    )
    return stable_hash(raw)


# ------------------------------- telegram ------------------------------------


def send_telegram(cfg: Config, text: str, dry_run: bool = False, chat_id: str | None = None) -> None:
    target_chat_id = str(chat_id or cfg.telegram_chat_id or "").strip()
    chunks = split_message(text)
    if dry_run or not (cfg.telegram_bot_token and target_chat_id):
        prefix = "[dry-run telegram]" if dry_run else "[telegram disabled: missing TELEGRAM_BOT_TOKEN/CHAT_ID]"
        for chunk in chunks:
            logging.info("%s chat_id=%s message_len=%s", prefix, target_chat_id or "-", len(chunk))
            print(f"{prefix} chat_id={target_chat_id or '-'}\n{render_for_terminal(chunk)}\n")
        return
    for chunk in chunks:
        payload = {
            "chat_id": target_chat_id,
            "text": chunk,
            "disable_web_page_preview": "true" if cfg.telegram_disable_web_page_preview else "false",
        }
        if cfg.telegram_parse_mode:
            payload["parse_mode"] = cfg.telegram_parse_mode
        try:
            telegram_api_request(cfg, "sendMessage", payload, timeout_seconds=20)
            logging.info("telegram sent: %s bytes to chat_id=%s", len(chunk.encode("utf-8")), target_chat_id)
        except Exception as exc:
            logging.error("telegram send failed: %s", exc)
            raise


def telegram_get_updates(cfg: Config, offset: int | None, timeout_seconds: int = 0) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {
        "timeout": str(max(0, int(timeout_seconds))),
        "allowed_updates": json.dumps(["message"]),
    }
    if offset is not None and offset > 0:
        payload["offset"] = str(offset)
    data = telegram_api_request(cfg, "getUpdates", payload, timeout_seconds=max(10, timeout_seconds + 10))
    result = data.get("result")
    if not isinstance(result, list):
        return []
    return [update for update in result if isinstance(update, dict)]


def telegram_api_request(cfg: Config, method: str, payload: dict[str, Any], timeout_seconds: int = 20) -> dict[str, Any]:
    if not cfg.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    url = f"{cfg.telegram_api_base}/bot{cfg.telegram_bot_token}/{method}"
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    parsed = json.loads(body)
    if not parsed.get("ok"):
        description = parsed.get("description") or "unknown telegram error"
        raise RuntimeError(f"telegram {method} failed: {description}")
    return parsed


def normalize_telegram_command(text: str) -> str:
    first = text.strip().split()[0].lower()
    if "@" in first:
        first = first.split("@", 1)[0]
    return first


def telegram_allowed_chat_id_set(cfg: Config) -> set[str]:
    raw = cfg.telegram_allowed_chat_ids or cfg.telegram_chat_id
    return {part.strip() for part in raw.split(",") if part.strip()}


def telegram_chat_allowed(cfg: Config, chat_id: str) -> bool:
    allowed = telegram_allowed_chat_id_set(cfg)
    return "*" in allowed or chat_id in allowed


def build_command_help() -> str:
    return "\n".join([
        "🤖 <b>Sub2API Monitor</b>",
        "",
        f"{tg_code('/status')} 当前账号状态",
        f"{tg_code('/daily')} 昨日/今日用量日报",
        f"{tg_code('/ping')} 检查 bot 是否在线",
        f"{tg_code('/help')} 显示帮助",
    ])


def build_unknown_command(command: str) -> str:
    return "\n".join([
        h(f"未知命令：{command}"),
        h("发送 /help 查看可用命令。"),
    ])


# -------------------------------- utilities -----------------------------------




def h(value: Any) -> str:
    return html.escape(str(value), quote=False)


def tg_code(value: Any) -> str:
    return f"<code>{h(value)}</code>"


def section(title: str) -> str:
    return f"<b>{h(title)}</b>"


def muted(text: str) -> str:
    return tg_code(text)


def render_for_terminal(message: str) -> str:
    # Make dry-run/menu output readable even when Telegram HTML formatting is enabled.
    rendered = re.sub(r"</?(?:b|strong|i|em|u|s|strike|del|code|pre)>", "", message)
    return html.unescape(rendered)


def short_time(value: Any) -> str:
    raw = fmt_time(value)
    if raw == "-":
        return raw
    # Prefer compact local-looking timestamps in Telegram rows.
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}:\d{2})", raw)
    if match:
        return f"{match.group(2)}-{match.group(3)} {match.group(4)}"
    return raw[:16]


def fmt_compact_int(value: Any) -> str:
    try:
        n = int(value or 0)
    except Exception:
        return "0"
    sign = "-" if n < 0 else ""
    n = abs(n)
    for suffix, unit in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if n >= unit:
            return f"{sign}{n / unit:.2f}{suffix}"
    return f"{sign}{n}"

def first_existing(paths: Iterable[str]) -> str | None:
    for p in paths:
        if Path(p).exists():
            return p
    return None


def read_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            env[key] = val
    except FileNotFoundError:
        pass
    except PermissionError:
        logging.warning("cannot read env file: %s", path)
    return env


def get_str(env: dict[str, str], key: str, default: str) -> str:
    value = env.get(key)
    return value if value not in (None, "") else default


def get_int(env: dict[str, str], key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(str(env.get(key, default)).strip())
    except Exception:
        return default
    return max(low, min(high, value))


def get_bool(env: dict[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        backup = path.with_suffix(path.suffix + f".bad.{int(time.time())}")
        path.rename(backup)
        logging.warning("state JSON was corrupt; moved to %s", backup)
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def setup_logging(cfg: Config, verbose: bool = False) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        log_path = Path(cfg.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    except Exception:
        pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def now_iso(tz: dt.tzinfo) -> str:
    return dt.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")


def fmt_time(value: Any) -> str:
    if value in (None, ""):
        return "-"
    return str(value).replace("T", " ")[:19]


def truncate(text: str, length: int) -> str:
    text = text.replace("\n", " ").replace("\r", " ")
    if len(text) <= length:
        return text
    return text[: max(0, length - 1)] + "…"


def fmt_int(value: Any) -> str:
    try:
        return f"{int(value or 0):,}"
    except Exception:
        return "0"


def fmt_money(value: Any) -> str:
    try:
        return f"{float(value or 0):.6f}"
    except Exception:
        return "0.000000"


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def clamp_message(text: str, limit: int = 3900) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n…（已截断）"


def split_message(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines():
        if current_len + len(line) + 1 > limit and current:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def short_exc() -> str:
    typ, exc, _ = sys.exc_info()
    if exc is None:
        return "unknown error"
    return truncate(f"{typ.__name__ if typ else 'Exception'}: {exc}", 500)


# ----------------------------------- CLI --------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Passive monitor for Sub2API + Telegram")
    p.add_argument("--config", help="config.env path")
    p.add_argument("--dry-run", action="store_true", help="print Telegram messages instead of sending")
    p.add_argument("--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("daemon", help="run monitor loop")
    once = sub.add_parser("run-once", help="run account + upstream checks once")
    once.add_argument("--notify", action="store_true", help="send Telegram if there are messages")
    once.add_argument("--no-daily", action="store_true", help="skip daily report scheduler")
    account = sub.add_parser("account-summary", help="print/send current account summary")
    account.add_argument("--notify", action="store_true", help="send current account summary to Telegram")
    daily = sub.add_parser("daily", help="build/send a daily report")
    daily.add_argument("--date", help="YYYY-MM-DD local day to report; default yesterday")
    daily.add_argument("--notify", action="store_true")
    sub.add_parser("test-telegram", help="send a Telegram test message")
    sub.add_parser("inspect", help="print current config/state paths and account summary JSON")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = Config.load(args.config)
    setup_logging(cfg, args.verbose)
    mon = Monitor(cfg, dry_run=args.dry_run)

    if args.command == "daemon":
        mon.daemon()
        return 0
    if args.command == "run-once":
        result = mon.run_once(notify=args.notify, include_daily=not args.no_daily)
        if not result.changed and not result.daily_message:
            print("No changes.")
        return 0
    if args.command == "account-summary":
        rows = mon.current_account_rows()
        msg = build_account_message(rows, [], [], [], cfg, title="ℹ️ sub2api 当前账号状态")
        if args.notify:
            mon.send(msg)
        else:
            print(render_for_terminal(msg))
        return 0
    if args.command == "daily":
        tz = cfg.tzinfo()
        if args.date:
            day = dt.date.fromisoformat(args.date)
        else:
            day = dt.datetime.now(tz).date() - dt.timedelta(days=1)
        msg = mon.build_daily_report(day)
        if args.notify:
            mon.send(msg)
        else:
            print(render_for_terminal(msg))
        return 0
    if args.command == "test-telegram":
        mon.send(f"✅ {APP_NAME} Telegram 测试成功\n时间：{now_iso(cfg.tzinfo())}")
        return 0
    if args.command == "inspect":
        rows = mon.current_account_rows()
        summary_lines, _ = summarize_accounts(rows)
        print(json.dumps({
            "config_path": cfg.config_path,
            "sub2api_dir": cfg.sub2api_dir,
            "postgres_container": cfg.postgres_container,
            "postgres_db": cfg.postgres_db,
            "state_file": cfg.state_file,
            "account_summary": summary_lines,
            "last_error_id": mon.state.get("last_error_id"),
        }, ensure_ascii=False, indent=2))
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
