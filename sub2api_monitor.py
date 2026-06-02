#!/usr/bin/env python3
"""Sub2API passive monitor with Telegram notifications.

This monitor intentionally does NOT probe upstream APIs. It only reads Sub2API's
own PostgreSQL tables (accounts, ops_error_logs, usage_logs) and reports:
- account availability/state changes;
- user recharge increases;
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
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

APP_NAME = "sub2api-monitor"

TELEGRAM_BOT_COMMANDS = [
    {"command": "status", "description": "健康概览"},
    {"command": "accounts", "description": "账号清单"},
    {"command": "groups", "description": "分组概览"},
    {"command": "daily", "description": "昨日/今日用量日报"},
    {"command": "update", "description": "检查程序更新"},
    {"command": "ping", "description": "检查 bot 是否在线"},
    {"command": "help", "description": "显示帮助"},
]
DEFAULT_UPDATE_REPO_URL = "https://github.com/jiwen77/sub2api-monitor.git"
DEFAULT_UPDATE_REF = "main"
UPDATE_CALLBACK_DATA = "sub2api:update:run"
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
    user_recharge_alerts_enabled: bool = True
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
    proxy_error_alerts_enabled: bool = True
    proxy_error_include_regex: str = (
        r"(?i)(proxy|socks|tunnel|http\s*connect|connect\s*failed|"
        r"network|timeout|timed\s*out|dns|enotfound|eai_again|"
        r"connection\s+(?:reset|refused)|econnreset|econnrefused|etimedout|"
        r"no\s+route|host\s+unreachable|tls|ssl|certificate)"
    )
    proxy_error_exclude_regex: str = (
        r"(?i)(api[\s_-]?key|user\s*key|invalid\s+key|unauthori[sz]ed|"
        r"forbidden|permission|authentication|invalidated\s+oauth\s+token|"
        r"failed\s+to\s+read\s+request\s+body|client\s+(?:closed|disconnect|cancel))"
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

        merged = {**sub_env, **env, **{k: v for k, v in os.environ.items() if k.startswith(("SUB2API_", "POSTGRES_", "TELEGRAM_", "USER_RECHARGE_"))}}
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
        cfg.user_recharge_alerts_enabled = get_bool(merged, "USER_RECHARGE_ALERTS_ENABLED", cfg.user_recharge_alerts_enabled)
        cfg.error_lookback_minutes = get_int(merged, "ERROR_LOOKBACK_MINUTES", cfg.error_lookback_minutes, 1, 1440)
        cfg.error_limit_per_poll = get_int(merged, "ERROR_LIMIT_PER_POLL", cfg.error_limit_per_poll, 1, 5000)
        cfg.error_cooldown_seconds = get_int(merged, "ERROR_COOLDOWN_SECONDS", cfg.error_cooldown_seconds, 0, 86400)
        cfg.upstream_allowed_status_codes = get_str(merged, "UPSTREAM_ALLOWED_STATUS_CODES", cfg.upstream_allowed_status_codes)
        cfg.upstream_exclude_regex = get_str(merged, "UPSTREAM_EXCLUDE_REGEX", cfg.upstream_exclude_regex)
        cfg.proxy_error_alerts_enabled = get_bool(merged, "PROXY_ERROR_ALERTS_ENABLED", cfg.proxy_error_alerts_enabled)
        cfg.proxy_error_include_regex = get_str(merged, "PROXY_ERROR_INCLUDE_REGEX", cfg.proxy_error_include_regex)
        cfg.proxy_error_exclude_regex = get_str(merged, "PROXY_ERROR_EXCLUDE_REGEX", cfg.proxy_error_exclude_regex)
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
    recharge_message: str | None = None
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
        self.state.setdefault("user_recharges", {})
        self.state.setdefault("last_error_id", 0)
        self.state.setdefault("error_cooldowns", {})
        self.allowed_status = parse_allowed_status(self.cfg.upstream_allowed_status_codes)
        self.exclude_re = re.compile(self.cfg.upstream_exclude_regex) if self.cfg.upstream_exclude_regex else None
        self.proxy_include_re = re.compile(self.cfg.proxy_error_include_regex) if self.cfg.proxy_error_include_regex else None
        self.proxy_exclude_re = re.compile(self.cfg.proxy_error_exclude_regex) if self.cfg.proxy_error_exclude_regex else None

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
            recharge_msg = self.check_user_recharges()
            if recharge_msg:
                result.recharge_message = recharge_msg
                result.changed = True
                if notify:
                    self.send(recharge_msg)
        except Exception:
            logging.exception("user recharge check failed")
            msg = build_simple_alert("⚠️ 用户充值检查失败", short_exc(), self.cfg)
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
        self.register_telegram_bot_commands()
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

    def register_telegram_bot_commands(self) -> None:
        if not self.telegram_commands_active():
            return
        try:
            set_telegram_bot_commands(self.cfg, dry_run=self.dry_run)
        except Exception:
            logging.exception("failed to register telegram bot commands")

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
        callback_query = update.get("callback_query")
        if isinstance(callback_query, dict):
            self.handle_telegram_callback(callback_query)
            return

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
            elif command in {"/status", "/account", "/summary"}:
                rows = self.current_account_rows()
                self.send(build_account_message(rows, [], [], [], self.cfg, title="🩺 sub2api 健康概览"), chat_id=chat_id)
            elif command in {"/accounts", "/status_all", "/all_status", "/allstatus", "/accounts_all", "/allaccounts"}:
                rows = self.current_account_rows()
                self.send(build_accounts_list_message(rows, self.cfg), chat_id=chat_id)
            elif command in {"/groups", "/group"}:
                rows = self.current_account_rows()
                self.send(build_groups_message(rows, self.cfg), chat_id=chat_id)
            elif command in {"/daily", "/report"}:
                tz = self.cfg.tzinfo()
                day = dt.datetime.now(tz).date() - dt.timedelta(days=1)
                self.send(self.build_daily_report(day), chat_id=chat_id)
            elif command in {"/update", "/upgrade"}:
                status = self.check_update_status()
                reply_markup = build_update_reply_markup() if status.get("has_update") else None
                self.send(build_update_status_message(status, self.cfg.tzinfo()), chat_id=chat_id, reply_markup=reply_markup)
            elif command == "/ping":
                self.send("🏓 <b>pong</b>\n" + muted(now_iso(self.cfg.tzinfo())), chat_id=chat_id)
            else:
                self.send(build_unknown_command(command), chat_id=chat_id)
        except Exception:
            logging.exception("telegram command handling failed: %s", command)
            self.send(build_simple_alert("⚠️ Telegram 命令执行失败", short_exc(), self.cfg), chat_id=chat_id)

    def handle_telegram_callback(self, callback_query: dict[str, Any]) -> None:
        callback_query_id = str(callback_query.get("id") or "")
        data = str(callback_query.get("data") or "")
        message = callback_query.get("message")
        chat_id = ""
        if isinstance(message, dict) and isinstance(message.get("chat"), dict):
            chat_id = str(message["chat"].get("id") or "").strip()
        if not chat_id or not telegram_chat_allowed(self.cfg, chat_id):
            logging.warning("ignored telegram callback from unauthorized chat_id=%s", chat_id or "-")
            if callback_query_id:
                self.answer_callback_query(callback_query_id, "无权限")
            return

        if data != UPDATE_CALLBACK_DATA:
            if callback_query_id:
                self.answer_callback_query(callback_query_id, "未知操作")
            return

        try:
            status = self.check_update_status()
            if status.get("error"):
                if callback_query_id:
                    self.answer_callback_query(callback_query_id, "检查失败")
                self.send(build_update_status_message(status, self.cfg.tzinfo()), chat_id=chat_id)
                return
            if not status.get("has_update"):
                if callback_query_id:
                    self.answer_callback_query(callback_query_id, "已是最新版")
                self.send(build_update_status_message(status, self.cfg.tzinfo()), chat_id=chat_id)
                return

            log_path = self.trigger_self_update()
            if callback_query_id:
                self.answer_callback_query(callback_query_id, "开始更新")
            self.send(build_update_triggered_message(status, log_path, self.cfg.tzinfo()), chat_id=chat_id)
        except Exception:
            logging.exception("telegram update callback failed")
            if callback_query_id:
                self.answer_callback_query(callback_query_id, "更新失败")
            self.send(build_simple_alert("⚠️ 程序更新触发失败", short_exc(), self.cfg), chat_id=chat_id)

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        if self.dry_run or not self.cfg.telegram_bot_token:
            logging.info("[dry-run callback answer] id=%s text=%s", callback_query_id, text)
            return
        answer_telegram_callback(self.cfg, callback_query_id, text)

    def check_update_status(self) -> dict[str, Any]:
        local_version = current_install_version()
        repo_url = os.environ.get("UPDATE_REPO_URL", DEFAULT_UPDATE_REPO_URL)
        ref = os.environ.get("UPDATE_REF", DEFAULT_UPDATE_REF)
        remote_version = ""
        error = ""
        try:
            remote_version = remote_git_version(repo_url, ref)
        except Exception as exc:
            error = truncate(str(exc), 300)
        has_update = bool(local_version and remote_version and local_version != remote_version)
        return {
            "local_version": local_version,
            "remote_version": remote_version,
            "repo_url": repo_url,
            "ref": ref,
            "has_update": has_update,
            "error": error,
        }

    def trigger_self_update(self) -> str:
        script = Path(__file__).resolve().parent / "monitor.sh"
        if not script.exists():
            raise RuntimeError(f"update script not found: {script}")
        log_path = Path(self.cfg.log_file).parent / "update-from-telegram.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        systemd_run = shutil.which("systemd-run")
        if systemd_run:
            unit = f"sub2api-monitor-self-update-{int(time.time())}"
            shell_cmd = f"exec {shlex.quote(str(script))} --update >> {shlex.quote(str(log_path))} 2>&1"
            proc = subprocess.run(
                [
                    systemd_run,
                    "--unit",
                    unit,
                    "--collect",
                    "--description",
                    "Sub2API Monitor Telegram self-update",
                    "/bin/bash",
                    "-lc",
                    shell_cmd,
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
            if proc.returncode == 0:
                return str(log_path)
            logging.warning("systemd-run update launch failed rc=%s: %s", proc.returncode, proc.stderr.strip()[:300])

        with log_path.open("ab") as log:
            subprocess.Popen(
                [str(script), "--update"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
            )
        return str(log_path)

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
WITH account_group_rows AS (
  SELECT
    ag.account_id,
    json_agg(
      json_build_object(
        'id', g.id,
        'name', coalesce(g.name, ''),
        'status', coalesce(g.status, ''),
        'platform', coalesce(g.platform, ''),
        'subscription_type', coalesce(g.subscription_type, ''),
        'priority', ag.priority
      )
      ORDER BY coalesce(ag.priority, 0) DESC, coalesce(g.sort_order, 0) ASC, g.id ASC
    ) AS groups
  FROM account_groups ag
  JOIN groups g ON g.id = ag.group_id
  WHERE g.deleted_at IS NULL
  GROUP BY ag.account_id
), rows AS (
  SELECT
    a.id,
    a.platform,
    a.type,
    coalesce(nullif(a.credentials->>'plan_type',''), nullif(a.extra->>'plan_type',''), a.type, 'unknown') AS plan,
    a.status,
    a.schedulable,
    (a.rate_limit_reset_at IS NOT NULL AND a.rate_limit_reset_at > now()) AS rate_limited,
    a.rate_limit_reset_at,
    (a.overload_until IS NOT NULL AND a.overload_until > now()) AS overloaded,
    a.overload_until,
    (a.temp_unschedulable_until IS NOT NULL AND a.temp_unschedulable_until > now()) AS temp_unschedulable,
    a.temp_unschedulable_until,
    coalesce(a.temp_unschedulable_reason, '') AS temp_unschedulable_reason,
    (a.auto_pause_on_expired AND a.expires_at IS NOT NULL AND a.expires_at <= now()) AS expired,
    a.expires_at,
    coalesce(a.error_message, '') AS error_message,
    a.last_used_at,
    a.updated_at,
    a.name,
    coalesce(a.credentials->>'email', a.extra->>'email', '') AS email,
    coalesce(agr.groups, '[]'::json) AS groups,
    CASE WHEN (a.extra->>'codex_5h_used_percent') ~ '^-?[0-9]+(\\.[0-9]+)?$'
      THEN round((a.extra->>'codex_5h_used_percent')::numeric, 2) ELSE NULL END AS codex_5h_used_percent,
    CASE WHEN (a.extra->>'codex_7d_used_percent') ~ '^-?[0-9]+(\\.[0-9]+)?$'
      THEN round((a.extra->>'codex_7d_used_percent')::numeric, 2) ELSE NULL END AS codex_7d_used_percent,
    CASE
      WHEN a.status = 'active'
       AND a.schedulable IS TRUE
       AND NOT (a.rate_limit_reset_at IS NOT NULL AND a.rate_limit_reset_at > now())
       AND NOT (a.overload_until IS NOT NULL AND a.overload_until > now())
       AND NOT (a.temp_unschedulable_until IS NOT NULL AND a.temp_unschedulable_until > now())
       AND NOT (a.auto_pause_on_expired AND a.expires_at IS NOT NULL AND a.expires_at <= now())
      THEN true ELSE false
    END AS normal
  FROM accounts a
  LEFT JOIN account_group_rows agr ON agr.account_id = a.id
  WHERE a.deleted_at IS NULL
  ORDER BY a.platform, plan, a.id
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
            if not account_digests_equal(prev[aid], current[aid]):
                row = dict(by_id[aid])
                row["_previous"] = prev[aid]
                changed.append(row)
        if not added and not removed and not changed:
            return None
        return build_account_message(
            rows,
            changed,
            added,
            removed,
            self.cfg,
            title="🔔 sub2api 账号状态变化",
            include_summary=False,
            include_abnormal=False,
        )

    def current_user_recharge_rows(self) -> list[dict[str, Any]]:
        sql = """
WITH rows AS (
  SELECT
    id,
    coalesce(email, '') AS email,
    coalesce(username, '') AS username,
    coalesce(role, '') AS role,
    coalesce(status, '') AS status,
    coalesce(balance, 0)::numeric(30,8)::text AS balance,
    coalesce(total_recharged, 0)::numeric(30,8)::text AS total_recharged,
    created_at,
    updated_at
  FROM users
  WHERE deleted_at IS NULL
  ORDER BY id
)
SELECT coalesce(json_agg(row_to_json(rows)), '[]'::json)::text FROM rows;
"""
        return self.query_json(sql) or []

    def check_user_recharges(self) -> str | None:
        if not self.cfg.user_recharge_alerts_enabled:
            return None
        rows = self.current_user_recharge_rows()
        now = now_iso(self.cfg.tzinfo())
        prev: dict[str, Any] = self.state.get("user_recharges") or {}
        initialized = bool(self.state.get("user_recharges_initialized"))
        current = {str(r["id"]): user_recharge_digest(r) for r in rows}
        self.state["user_recharges"] = current
        self.state["user_recharges_updated_at"] = now
        self.state["user_recharges_initialized"] = True

        if not initialized:
            return None

        by_id = rows_by_id(rows)
        recharges: list[dict[str, Any]] = []
        for uid in sorted(current.keys(), key=lambda x: int(x) if x.isdigit() else x):
            previous_total = decimal_value((prev.get(uid) or {}).get("total_recharged"))
            current_total = decimal_value(current[uid].get("total_recharged"))
            if current_total <= previous_total:
                continue
            row = dict(by_id[uid])
            row["_previous_total_recharged"] = decimal_string(previous_total)
            row["_recharge_delta"] = decimal_string(current_total - previous_total)
            recharges.append(row)

        if not recharges:
            return None
        recharges.sort(key=lambda r: decimal_value(r.get("_recharge_delta")), reverse=True)
        return build_user_recharge_message(recharges, self.cfg)

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
    e.id,
    e.created_at,
    e.platform,
    coalesce(nullif(e.requested_model,''), nullif(e.upstream_model,''), nullif(e.model,''), '') AS model,
    e.status_code,
    e.upstream_status_code,
    coalesce(e.error_type, '') AS error_type,
    coalesce(e.provider_error_type, '') AS provider_error_type,
    coalesce(e.provider_error_code, '') AS provider_error_code,
    coalesce(e.network_error_type, '') AS network_error_type,
    coalesce(e.error_owner, '') AS error_owner,
    coalesce(e.error_source, '') AS error_source,
    coalesce(e.is_business_limited, false) AS is_business_limited,
    coalesce(nullif(e.upstream_error_message,''), nullif(e.error_message,''), nullif(e.provider_error_type,''), '') AS message,
    e.request_id,
    e.client_request_id,
    e.request_path,
    e.account_id,
    coalesce(a.name, '') AS account_name,
    coalesce(nullif(a.credentials->>'email',''), nullif(a.extra->>'email',''), '') AS account_email,
    coalesce(nullif(a.credentials->>'plan_type',''), nullif(a.extra->>'plan_type',''), a.type, '') AS account_plan,
    coalesce(a.status, '') AS account_status,
    e.upstream_endpoint,
    a.proxy_id,
    coalesce(p.name, '') AS proxy_name,
    coalesce(p.status, '') AS proxy_status,
    coalesce(p.protocol, '') AS proxy_protocol
  FROM ops_error_logs e
  LEFT JOIN accounts a ON a.id = e.account_id
  LEFT JOIN proxies p ON p.id = a.proxy_id
  WHERE e.id > {last_id}
    AND e.created_at >= now() - interval '{lookback} minutes'
  ORDER BY e.id ASC
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

    def is_actionable_proxy_error(self, row: dict[str, Any]) -> bool:
        if not self.cfg.proxy_error_alerts_enabled:
            return False
        if row.get("is_business_limited") is True:
            return False
        if row.get("error_owner") == "client":
            return False
        source = str(row.get("error_source") or "")
        message = str(row.get("message") or "")
        network_type = str(row.get("network_error_type") or "")
        haystack = " ".join([message, network_type, str(row.get("error_type") or ""), str(row.get("provider_error_type") or "")])
        if self.proxy_exclude_re and self.proxy_exclude_re.search(haystack):
            return False
        if source and not source.startswith("upstream") and not network_type:
            return False
        if network_type:
            return True
        return bool(self.proxy_include_re and self.proxy_include_re.search(haystack))

    def check_upstream_errors(self) -> str | None:
        rows = self.fetch_new_error_rows()
        if not rows:
            return None
        max_id = max(int(r.get("id") or 0) for r in rows)
        proxy_errors = [r for r in rows if self.is_actionable_proxy_error(r)]
        proxy_error_ids = {int(r.get("id") or 0) for r in proxy_errors}
        upstream_errors = [r for r in rows if int(r.get("id") or 0) not in proxy_error_ids and self.is_actionable_upstream_error(r)]
        self.state["last_error_id"] = max(max_id, int(self.state.get("last_error_id") or 0))
        if not upstream_errors and not proxy_errors:
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

        upstream_grouped, upstream_suppressed = group_error_rows(
            upstream_errors, cooldowns, now_ts, self.cfg.error_cooldown_seconds, error_key, "upstream"
        )
        proxy_grouped, proxy_suppressed = group_error_rows(
            proxy_errors, cooldowns, now_ts, self.cfg.error_cooldown_seconds, proxy_error_key, "proxy"
        )

        messages = []
        if upstream_grouped:
            messages.append(build_error_message(upstream_grouped, upstream_suppressed, self.cfg))
        if proxy_grouped:
            messages.append(build_proxy_error_message(proxy_grouped, proxy_suppressed, self.cfg))
        if not messages:
            logging.info("ops errors suppressed by cooldown: upstream=%s proxy=%s", upstream_suppressed, proxy_suppressed)
            return None
        return "\n\n".join(messages)

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

    def send(self, text: str, chat_id: str | None = None, reply_markup: dict[str, Any] | None = None) -> None:
        send_telegram(self.cfg, text, dry_run=self.dry_run, chat_id=chat_id, reply_markup=reply_markup)


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
        "groups": account_groups_digest(row),
    }


def account_digests_equal(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    # Backward-compatible state migration: monitors upgraded from versions before
    # account groups were tracked should not emit a one-time "all accounts changed"
    # alert just because the persisted digest lacks the new groups key.
    prev = dict(previous or {})
    if "groups" not in prev and "groups" in current:
        prev["groups"] = current.get("groups")
    return prev == current


def account_groups_digest(row: dict[str, Any]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for group in normalized_account_groups(row):
        groups.append(
            {
                "id": group.get("id"),
                "name": truncate(str(group.get("name") or ""), 80),
                "status": truncate(str(group.get("status") or ""), 40),
            }
        )
    return groups


def user_recharge_digest(row: dict[str, Any]) -> dict[str, Any]:
    # Keep the persisted state non-sensitive: only numeric baselines are needed
    # to detect future recharge increases.
    return {
        "id": row.get("id"),
        "balance": decimal_string(row.get("balance")),
        "total_recharged": decimal_string(row.get("total_recharged")),
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
    include_summary: bool = True,
    include_abnormal: bool = True,
    include_all: bool = False,
    clamp: bool = True,
) -> str:
    _summary_lines, buckets = summarize_accounts(rows)
    lines = [
        f"{h(title)}",
        muted(now_iso(cfg.tzinfo())),
    ]
    if include_summary:
        lines += ["", section("账号概览")]
        for key, bucket in sorted(buckets.items()):
            lines.append(format_summary_line(key, bucket, html_mode=True))

    abnormal = sorted([r for r in rows if not r.get("normal")], key=account_sort_key)
    if changed or added or removed:
        lines += [
            "",
            section("本次变化（上一轮 → 当前）"),
            h(f"{len(changed)} 个变更 / {len(added)} 个新增 / {len(removed)} 个移除"),
        ]
        shown = 0
        for row in changed[: cfg.detail_limit]:
            lines.append(format_account_change_row(row, cfg))
            shown += 1
        for row in added[: cfg.detail_limit]:
            lines.append(format_account_row(row, cfg, prefix="➕ 新增"))
            shown += 1
        for old in removed[: cfg.detail_limit]:
            previous_state = describe_account_state(old, include_error=False)
            removed_lines = [
                f"➖ {h('移除')} {tg_code('#' + str(old.get('id')))} "
                f"{tg_code(str(old.get('platform') or 'unknown') + '/' + str(old.get('plan') or 'unknown'))}"
                f"\n  {h('移除前：')} {h(previous_state)}"
            ]
            groups = account_groups_summary(old)
            if groups:
                removed_lines.append(f"  {h('所属分组：' + groups)}")
            lines.append("\n".join(removed_lines))
            shown += 1
        total_changes = len(changed) + len(added) + len(removed)
        if total_changes > shown:
            lines.append(muted(f"另有 {total_changes - shown} 条变化未展开"))

    if include_abnormal and abnormal:
        lines += ["", section("当前需要关注（非正常账号）")]
        for row in abnormal[: cfg.detail_limit]:
            lines.append(format_account_row(row, cfg))
        if len(abnormal) > cfg.detail_limit:
            lines.append(muted(f"另有 {len(abnormal) - cfg.detail_limit} 个非正常账号未展开"))

    if include_all:
        lines += ["", section(f"全部账号状态（{len(rows)} 个）")]
        if rows:
            for row in rows:
                lines.append(format_account_row(row, cfg))
        else:
            lines.append(muted("暂无账号"))

    text = "\n".join(lines)
    return clamp_message(text) if clamp else text


def build_accounts_list_message(rows: list[dict[str, Any]], cfg: Config) -> str:
    lines = [
        "📋 <b>sub2api 账号清单</b>",
        muted(now_iso(cfg.tzinfo())),
        "",
        section(f"账号清单（{len(rows)} 个）"),
    ]
    if rows:
        for row in rows:
            lines.append(format_account_row(row, cfg))
    else:
        lines.append(muted("暂无账号"))
    return "\n".join(lines)


def build_groups_message(rows: list[dict[str, Any]], cfg: Config) -> str:
    buckets = summarize_account_groups(rows)
    lines = [
        "🧩 <b>sub2api 分组概览</b>",
        muted(now_iso(cfg.tzinfo())),
        "",
        section(f"分组健康（{len(buckets)} 个）"),
    ]
    if buckets:
        for bucket in buckets[: cfg.detail_limit]:
            lines.append(format_group_summary_line(bucket))
        if len(buckets) > cfg.detail_limit:
            lines.append(muted(f"另有 {len(buckets) - cfg.detail_limit} 个分组未展开"))
    else:
        lines.append(muted("暂无分组"))

    abnormal_rows = sorted([row for row in rows if not row.get("normal")], key=account_sort_key)
    if abnormal_rows:
        lines += ["", section(f"需要关注的账号（去重，{len(abnormal_rows)} 个）")]
        for row in abnormal_rows[: cfg.detail_limit]:
            lines.append(format_account_row(row, cfg))
        if len(abnormal_rows) > cfg.detail_limit:
            lines.append(muted(f"另有 {len(abnormal_rows) - cfg.detail_limit} 个非正常账号未展开"))
    else:
        lines += ["", muted("当前没有非正常分组账号")]
    return clamp_message("\n".join(lines))


def build_user_recharge_message(rows: list[dict[str, Any]], cfg: Config) -> str:
    total_delta = sum((decimal_value(row.get("_recharge_delta")) for row in rows), Decimal("0"))
    lines = [
        "💰 <b>Sub2API 用户充值</b>",
        muted(now_iso(cfg.tzinfo())),
        h(f"新增 {len(rows)} 笔 / 合计 +{fmt_amount(total_delta)}"),
        "",
        section("充值明细"),
    ]
    for row in rows[: cfg.detail_limit]:
        ident = tg_code(f"#{row.get('id')}")
        name = display_user_identifier(row, cfg)
        meta_bits = [str(row.get("role") or "").strip(), str(row.get("status") or "").strip()]
        meta = ", ".join(bit for bit in meta_bits if bit)
        head_bits = ["•", ident]
        if name:
            head_bits.append(h(name))
        if meta:
            head_bits.append(h(f"({meta})"))
        lines.append(" ".join(head_bits))

        detail = (
            f"充值：+{fmt_amount(row.get('_recharge_delta'))}"
            f" · 累计：{fmt_amount(row.get('total_recharged'))}"
            f" · 余额：{fmt_amount(row.get('balance'))}"
        )
        lines.append(f"  {h(detail)}")
        if row.get("updated_at"):
            lines.append(f"  {h('更新时间：' + short_time(row.get('updated_at')))}")
    if len(rows) > cfg.detail_limit:
        lines.append(muted(f"另有 {len(rows) - cfg.detail_limit} 笔充值未展开"))
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


def summarize_account_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        groups = normalized_account_groups(row)
        if not groups:
            groups = [{"id": "__ungrouped__", "name": "未分组", "status": ""}]
        for group in groups:
            key = account_group_key(group)
            bucket = buckets.setdefault(
                key,
                {
                    "group": group,
                    "normal": 0,
                    "total": 0,
                    "rate_limited": 0,
                    "error": 0,
                    "overloaded": 0,
                    "temp": 0,
                    "expired": 0,
                    "rows": [],
                    "abnormal_rows": [],
                },
            )
            bucket["total"] += 1
            bucket["rows"].append(row)
            if row.get("normal"):
                bucket["normal"] += 1
            else:
                bucket["abnormal_rows"].append(row)
            if row.get("rate_limited"):
                bucket["rate_limited"] += 1
            if row.get("status") not in ("active", ""):
                bucket["error"] += 1
            if row.get("overloaded"):
                bucket["overloaded"] += 1
            if row.get("temp_unschedulable"):
                bucket["temp"] += 1
            if row.get("expired"):
                bucket["expired"] += 1
    return sorted(buckets.values(), key=group_bucket_sort_key)


def account_group_key(group: dict[str, Any]) -> str:
    group_id = group.get("id")
    if group_id not in (None, ""):
        return "id:" + str(group_id)
    return "name:" + str(group.get("name") or "")


def group_bucket_sort_key(bucket: dict[str, Any]) -> tuple[int, str]:
    total = int(bucket.get("total") or 0)
    normal = int(bucket.get("normal") or 0)
    severity = 0 if total and normal < total else 1
    return severity, group_bucket_label(bucket).lower()


def format_group_summary_line(bucket: dict[str, Any]) -> str:
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
    line = f"{icon} {tg_code(group_bucket_label(bucket))} {normal}/{total}"
    return line + (f" · {h(' · '.join(extras))}" if extras else "")


def group_bucket_label(bucket: dict[str, Any]) -> str:
    return account_group_label(bucket.get("group") or {}) or "未分组"


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
    lines = [" ".join(head_bits), "  " + h(" · ").join(second_bits)]
    groups = account_groups_summary(row)
    if groups:
        lines.append(f"  {h('所属分组：' + groups)}")
    return "\n".join(lines)


def format_account_inline_row(row: dict[str, Any], cfg: Config) -> str:
    icon = account_icon(row)
    label = f"{row.get('platform') or 'unknown'}/{row.get('plan') or 'unknown'}"
    ident = tg_code(f"#{row.get('id')}")
    name = display_identifier(row, cfg)
    head_bits = [icon, ident]
    if name:
        head_bits.append(h(name))
    head_bits.append(tg_code(label))

    detail = describe_account_state(row, include_error=True)
    quota = plain_account_quota_summary(row)
    if quota:
        detail += f" · {quota}"
    return " ".join(head_bits) + f" — {h(detail)}"



def format_account_change_row(row: dict[str, Any], cfg: Config) -> str:
    previous = row.get("_previous") or {}
    icon = account_icon(row)
    label = f"{row.get('platform') or 'unknown'}/{row.get('plan') or 'unknown'}"
    ident = tg_code(f"#{row.get('id')}")
    name = display_identifier(row, cfg)
    head_bits = ["🔄", icon, ident]
    if name:
        head_bits.append(h(name))
    head_bits.append(tg_code(label))

    details: list[str] = []
    old_label = f"{previous.get('platform') or 'unknown'}/{previous.get('plan') or 'unknown'}"
    if old_label != label:
        details.append(f"平台/套餐：{old_label} → {label}")
    old_type = str(previous.get("type") or "")
    new_type = str(row.get("type") or "")
    if old_type and new_type and old_type != new_type:
        details.append(f"类型：{old_type} → {new_type}")

    before = describe_account_state(previous, include_error=False)
    after = describe_account_state(row, include_error=True)
    if before != after:
        details.append(f"状态：{before} → {after}")
    elif previous.get("error_message_hash") and previous.get("error_message_hash") != account_digest(row).get("error_message_hash"):
        details.append(f"状态：{before} → {after}（错误详情变化）")
    elif not details:
        details.append(f"状态细节更新：{after}")

    quota = plain_account_quota_summary(row)
    if quota:
        details.append(f"当前用量：{quota}")

    old_groups = account_groups_summary(previous)
    new_groups = account_groups_summary(row)
    if old_groups or new_groups:
        if old_groups and old_groups != new_groups:
            details.append(f"所属分组：{old_groups} → {new_groups or '未分组'}")
        else:
            details.append(f"所属分组：{new_groups or old_groups}")

    return " ".join(head_bits) + "\n  " + "\n  ".join(h(detail) for detail in details)

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
    return h(describe_account_state(row, include_error=True))


def describe_account_state(row: dict[str, Any], include_error: bool = True) -> str:
    if row.get("status") not in ("active", ""):
        err = clean_account_error(str(row.get("error_message") or "")) if include_error else ""
        return f"异常：{row.get('status') or 'unknown'}" + (f" · {err}" if err else "")
    if row.get("rate_limited"):
        return f"限流至 {short_time(row.get('rate_limit_reset_at'))}"
    if row.get("overloaded"):
        return f"过载至 {short_time(row.get('overload_until'))}"
    if row.get("temp_unschedulable"):
        reason = clean_account_error(str(row.get("temp_unschedulable_reason") or ""))
        return f"临停至 {short_time(row.get('temp_unschedulable_until'))}" + (f" · {reason}" if reason else "")
    if row.get("expired"):
        return f"已过期 {short_time(row.get('expires_at'))}"
    if not row.get("schedulable"):
        return "不可调度"
    if row.get("normal"):
        return "正常"
    return "非正常"


def plain_account_quota_summary(row: dict[str, Any]) -> str:
    parts = []
    if row.get("codex_5h_used_percent") is not None:
        parts.append(f"5h {row.get('codex_5h_used_percent')}%")
    if row.get("codex_7d_used_percent") is not None:
        parts.append(f"7d {row.get('codex_7d_used_percent')}%")
    return " · ".join(parts)


def account_quota_summary(row: dict[str, Any]) -> str:
    return h(plain_account_quota_summary(row))


def normalized_account_groups(row: dict[str, Any]) -> list[dict[str, Any]]:
    raw = row.get("groups")
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return [{"id": None, "name": raw, "status": ""}] if raw.strip() else []
    if not isinstance(raw, list):
        return []

    groups: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            groups.append(item)
        elif item not in (None, ""):
            groups.append({"id": None, "name": str(item), "status": ""})
    return groups


def account_groups_summary(row: dict[str, Any], max_groups: int = 5, max_chars: int = 96) -> str:
    groups = normalized_account_groups(row)
    if not groups:
        return ""

    labels: list[str] = []
    used = 0
    for group in groups:
        label = account_group_label(group)
        if not label:
            continue
        next_used = used + len(label) + (1 if labels else 0)
        if labels and (len(labels) >= max_groups or next_used > max_chars):
            break
        labels.append(label)
        used = next_used

    if not labels:
        return ""
    hidden = max(0, len(groups) - len(labels))
    summary = "、".join(labels)
    if hidden:
        summary += f" 等 {len(groups)} 个"
    return summary


def account_group_label(group: dict[str, Any]) -> str:
    name = truncate(str(group.get("name") or "").strip(), 28)
    group_id = group.get("id")
    label = name or (f"#{group_id}" if group_id not in (None, "") else "")
    if not label:
        return ""
    status = str(group.get("status") or "").strip()
    if status and status != "active":
        label += f"({truncate(status, 16)})"
    return label


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


def display_user_identifier(row: dict[str, Any], cfg: Config) -> str:
    return display_identifier({"email": row.get("email"), "name": row.get("username")}, cfg)


def group_error_rows(
    rows: list[dict[str, Any]],
    cooldowns: dict[str, Any],
    now_ts: int,
    cooldown_seconds: int,
    key_func: Callable[[dict[str, Any]], str],
    namespace: str,
) -> tuple[dict[str, dict[str, Any]], int]:
    grouped: dict[str, dict[str, Any]] = {}
    suppressed = 0
    for row in rows:
        key = f"{namespace}:{key_func(row)}"
        if key not in grouped:
            if cooldown_seconds > 0 and key in cooldowns:
                suppressed += 1
                continue
            if cooldown_seconds > 0:
                cooldowns[key] = now_ts + cooldown_seconds
        g = grouped.setdefault(key, {"count": 0, "rows": [], "sample": row})
        g["count"] += 1
        g["rows"].append(row)
    return grouped, suppressed


def error_context_lines(group: dict[str, Any], cfg: Config, include_proxy: bool = True) -> list[str]:
    sample = group["sample"]
    rows = group.get("rows", [])
    lines: list[str] = []
    account_labels = grouped_account_labels(rows, cfg, limit=3)
    if account_labels:
        lines.append("账号：" + "；".join(account_labels))
    if include_proxy:
        proxy_label = proxy_display_label(sample)
        if proxy_label:
            lines.append("代理：" + proxy_label)
    return lines


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
        msg = truncate(re.sub(r"\s+", " ", str(sample.get("message") or "")).strip(), 96)
        lines.append(
            f"🔴 {tg_code(str(sample.get('platform') or 'unknown') + '/' + str(model))} "
            f"{tg_code(str(status))} ×{int(group['count'])}"
        )
        for context_line in error_context_lines(group, cfg, include_proxy=True):
            lines.append(f"  {h(context_line)}")
        if msg:
            lines.append(f"  {h('原因：' + msg)}")
    if len(grouped) > cfg.detail_limit:
        lines.append(muted(f"另有 {len(grouped) - cfg.detail_limit} 组未展开"))
    return clamp_message("\n".join(lines))


def display_error_account(row: dict[str, Any], cfg: Config) -> str:
    account_id = row.get("account_id")
    name_row = {"name": row.get("account_name"), "email": row.get("account_email")}
    name = display_identifier(name_row, cfg)
    plan = str(row.get("account_plan") or "").strip()
    status = str(row.get("account_status") or "").strip()
    bits = []
    if account_id:
        bits.append(f"#{account_id}")
    if name:
        bits.append(name)
    extras = []
    if plan:
        extras.append(plan)
    if status:
        extras.append(status)
    if extras:
        bits.append("(" + ", ".join(extras) + ")")
    return " ".join(bits)


def grouped_account_labels(rows: list[dict[str, Any]], cfg: Config, limit: int = 5) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for row in rows:
        label = display_error_account(row, cfg)
        if not label:
            continue
        key = str(row.get("account_id") or label)
        if key in seen:
            continue
        seen.add(key)
        labels.append(label)
        if len(labels) >= limit:
            break
    return labels


def grouped_request_ids(rows: list[dict[str, Any]], limit: int = 3) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for row in rows:
        raw = str(row.get("request_id") or row.get("client_request_id") or "").strip()
        if not raw or raw in seen:
            continue
        seen.add(raw)
        values.append(truncate(raw, 18))
        if len(values) >= limit:
            break
    return values


def proxy_display_label(row: dict[str, Any]) -> str:
    name = str(row.get("proxy_name") or "").strip()
    proxy_id = row.get("proxy_id")
    protocol = str(row.get("proxy_protocol") or "").strip()
    status = str(row.get("proxy_status") or "").strip()
    if name:
        label = name
    elif proxy_id:
        label = f"#{proxy_id}"
    else:
        return ""
    details = []
    if protocol:
        details.append(protocol)
    if status:
        details.append(status)
    return label + (f" ({', '.join(details)})" if details else "")


def build_proxy_error_message(grouped: dict[str, dict[str, Any]], suppressed: int, cfg: Config) -> str:
    total = sum(int(g["count"]) for g in grouped.values())
    lines = [
        "🌐 <b>Sub2API 出口/网络错误</b>",
        muted(now_iso(cfg.tzinfo())),
        h(f"新增 {total} 条 / {len(grouped)} 组") + (h(f" · 冷却抑制 {suppressed} 条") if suppressed else ""),
        "",
        section("错误分组"),
    ]
    for group in sorted(grouped.values(), key=lambda g: -int(g["count"]))[: cfg.detail_limit]:
        sample = group["sample"]
        model = sample.get("model") or "unknown-model"
        kind = sample.get("network_error_type") or sample.get("error_type") or sample.get("upstream_status_code") or sample.get("status_code") or "network"
        msg = truncate(re.sub(r"\s+", " ", str(sample.get("message") or "")).strip(), 96)
        lines.append(
            f"🟠 {tg_code(str(sample.get('platform') or 'unknown') + '/' + str(model))} "
            f"{tg_code(str(kind))} ×{int(group['count'])}"
        )
        for context_line in error_context_lines(group, cfg, include_proxy=True):
            lines.append(f"  {h(context_line)}")
        if msg:
            lines.append(f"  {h('原因：' + msg)}")
    if len(grouped) > cfg.detail_limit:
        lines.append(muted(f"另有 {len(grouped) - cfg.detail_limit} 组未展开"))
    return clamp_message("\n".join(lines))


def build_daily_message(day: dt.date, yesterday: dict[str, Any], today_date: dt.date, today: dict[str, Any], cfg: Config, now: dt.datetime) -> str:
    lines = [
        "📊 <b>Sub2API 每日用量</b>",
        muted(now.strftime('%Y-%m-%d %H:%M:%S %Z')),
        "",
    ]
    lines += daily_usage_section(f"昨日 {day.isoformat()}", "昨日", yesterday, cfg)
    lines += [""] + daily_usage_section(f"今日 {today_date.isoformat()} 截至当前", "今日", today, cfg)
    return clamp_message("\n".join(lines))


def daily_usage_section(title: str, label: str, stats: dict[str, Any], cfg: Config) -> list[str]:
    summary = stats.get("summary") or {}
    lines = [
        section(title),
        f"Tokens {tg_code(fmt_compact_int(summary.get('total_tokens')))} · Requests {tg_code(fmt_int(summary.get('requests')))} · Cost {tg_code(fmt_money(summary.get('total_cost')))}",
        f"Input {tg_code(fmt_compact_int(summary.get('input_tokens')))} · Output {tg_code(fmt_compact_int(summary.get('output_tokens')))} · Cache {tg_code(fmt_compact_int(summary.get('cache_tokens')))}",
        f"Avg {tg_code(str(summary.get('avg_duration_ms', 0)) + ' ms')} · First token {tg_code(str(summary.get('avg_first_token_ms', 0)) + ' ms')}",
    ]
    if stats.get("by_plan"):
        lines += ["", section(f"{label}按账号类型")]
        for row in stats["by_plan"][: cfg.detail_limit]:
            lines.append(
                f"• {tg_code(str(row.get('platform') or 'unknown') + '/' + str(row.get('plan') or 'unknown'))} "
                f"{h(fmt_compact_int(row.get('total_tokens')) + ' tokens')} · {h(fmt_int(row.get('requests')) + ' req')}"
            )
    if stats.get("top_models"):
        lines += ["", section(f"{label} Top 模型")]
        for row in stats["top_models"][: min(8, cfg.detail_limit)]:
            lines.append(
                f"• {tg_code(str(row.get('model') or 'unknown'))} "
                f"{h(fmt_compact_int(row.get('total_tokens')) + ' tokens')} · {h(fmt_int(row.get('requests')) + ' req')}"
            )
    return lines


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


def proxy_error_key(row: dict[str, Any]) -> str:
    msg = re.sub(r"\s+", " ", str(row.get("message") or "")).strip().lower()[:160]
    raw = "|".join(
        str(x or "")
        for x in (
            row.get("platform"),
            row.get("model"),
            row.get("proxy_id") or row.get("proxy_name"),
            row.get("network_error_type"),
            row.get("error_type"),
            row.get("provider_error_type"),
            row.get("upstream_endpoint"),
            msg,
        )
    )
    return stable_hash(raw)


# ------------------------------- telegram ------------------------------------


def send_telegram(
    cfg: Config,
    text: str,
    dry_run: bool = False,
    chat_id: str | None = None,
    reply_markup: dict[str, Any] | None = None,
) -> None:
    target_chat_id = str(chat_id or cfg.telegram_chat_id or "").strip()
    chunks = split_message(text)
    if dry_run or not (cfg.telegram_bot_token and target_chat_id):
        prefix = "[dry-run telegram]" if dry_run else "[telegram disabled: missing TELEGRAM_BOT_TOKEN/CHAT_ID]"
        for chunk in chunks:
            logging.info("%s chat_id=%s message_len=%s", prefix, target_chat_id or "-", len(chunk))
            print(f"{prefix} chat_id={target_chat_id or '-'}\n{render_for_terminal(chunk)}\n")
        return
    for index, chunk in enumerate(chunks):
        payload = {
            "chat_id": target_chat_id,
            "text": chunk,
            "disable_web_page_preview": "true" if cfg.telegram_disable_web_page_preview else "false",
        }
        if cfg.telegram_parse_mode:
            payload["parse_mode"] = cfg.telegram_parse_mode
        if reply_markup and index == 0:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        try:
            telegram_api_request(cfg, "sendMessage", payload, timeout_seconds=20)
            logging.info("telegram sent: %s bytes to chat_id=%s", len(chunk.encode("utf-8")), target_chat_id)
        except Exception as exc:
            logging.error("telegram send failed: %s", exc)
            raise


def set_telegram_bot_commands(cfg: Config, dry_run: bool = False) -> None:
    if dry_run or not cfg.telegram_bot_token:
        prefix = "[dry-run telegram commands]" if dry_run else "[telegram disabled: missing TELEGRAM_BOT_TOKEN]"
        print(prefix)
        for command in TELEGRAM_BOT_COMMANDS:
            print(f"/{command['command']} - {command['description']}")
        return
    payload = {"commands": json.dumps(TELEGRAM_BOT_COMMANDS, ensure_ascii=False)}
    telegram_api_request(cfg, "setMyCommands", payload, timeout_seconds=20)
    logging.info("telegram bot commands registered: %s", ",".join("/" + c["command"] for c in TELEGRAM_BOT_COMMANDS))


def telegram_get_updates(cfg: Config, offset: int | None, timeout_seconds: int = 0) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {
        "timeout": str(max(0, int(timeout_seconds))),
        "allowed_updates": json.dumps(["message", "callback_query"]),
    }
    if offset is not None and offset > 0:
        payload["offset"] = str(offset)
    data = telegram_api_request(cfg, "getUpdates", payload, timeout_seconds=max(10, timeout_seconds + 10))
    result = data.get("result")
    if not isinstance(result, list):
        return []
    return [update for update in result if isinstance(update, dict)]


def answer_telegram_callback(cfg: Config, callback_query_id: str, text: str = "") -> None:
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = truncate(text, 200)
    telegram_api_request(cfg, "answerCallbackQuery", payload, timeout_seconds=10)


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
    lines = ["🤖 <b>Sub2API Monitor</b>", ""]
    for command in TELEGRAM_BOT_COMMANDS:
        lines.append(f"{tg_code('/' + command['command'])} {h(command['description'])}")
    return "\n".join(lines)


def build_update_reply_markup() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "立即更新", "callback_data": UPDATE_CALLBACK_DATA}],
        ]
    }


def build_update_status_message(status: dict[str, Any], tz: dt.tzinfo | None = None) -> str:
    local_version = short_git_version(status.get("local_version"))
    remote_version = short_git_version(status.get("remote_version"))
    ref = str(status.get("ref") or DEFAULT_UPDATE_REF)
    lines = [
        "🔄 <b>Sub2API Monitor 更新检查</b>",
        muted(now_iso(tz or Config().tzinfo())),
        h(f"分支/标签：{ref}"),
        h(f"当前版本：{local_version}"),
    ]
    if status.get("error"):
        lines += ["", h("检查失败：" + str(status.get("error")))]
    elif status.get("has_update"):
        lines += [
            h(f"远端版本：{remote_version}"),
            "",
            h("发现新版本，点击下方按钮开始更新。"),
        ]
    else:
        lines += [
            h(f"远端版本：{remote_version}"),
            "",
            h("当前已是最新版本。"),
        ]
    return "\n".join(lines)


def build_update_triggered_message(status: dict[str, Any], log_path: str, tz: dt.tzinfo | None = None) -> str:
    return "\n".join([
        "🚀 <b>已触发更新</b>",
        muted(now_iso(tz or Config().tzinfo())),
        h(f"{short_git_version(status.get('local_version'))} → {short_git_version(status.get('remote_version'))}"),
        h(f"日志：{log_path}"),
        h("服务会由更新脚本自动重启。"),
    ])


def current_install_version() -> str:
    version_file = Path(__file__).resolve().parent / ".version"
    try:
        value = version_file.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[0-9a-f]{40}", value):
            return value
    except FileNotFoundError:
        pass
    except Exception:
        logging.exception("failed to read version file: %s", version_file)

    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        value = proc.stdout.strip()
        if proc.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", value):
            return value
    except Exception:
        logging.exception("failed to read git version")
    return "unknown"


def remote_git_version(repo_url: str, ref: str) -> str:
    proc = subprocess.run(
        ["git", "ls-remote", repo_url, ref],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git ls-remote failed rc={proc.returncode}: {proc.stderr.strip()[:300]}")
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == f"refs/heads/{ref}":
            return parts[0]
        if len(parts) >= 2 and parts[1] == ref:
            return parts[0]
    first = proc.stdout.split()
    if first and re.fullmatch(r"[0-9a-f]{40}", first[0]):
        return first[0]
    raise RuntimeError(f"remote ref not found: {shlex.quote(ref)}")


def short_git_version(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "unknown"
    if len(text) >= 8 and re.fullmatch(r"[0-9a-fA-F]{8,40}", text):
        return text[:8]
    return text


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


def decimal_value(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return Decimal("0")


def decimal_string(value: Any) -> str:
    return format(decimal_value(value), "f")


def fmt_amount(value: Any) -> str:
    value_dec = decimal_value(value)
    text = f"{value_dec:,.6f}".rstrip("0").rstrip(".")
    return text or "0"


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
    sub.add_parser("setup-telegram-commands", help="register Telegram slash-command menu")
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
        set_telegram_bot_commands(cfg, dry_run=args.dry_run)
        mon.send(f"✅ {APP_NAME} Telegram 测试成功\n时间：{now_iso(cfg.tzinfo())}")
        return 0
    if args.command == "setup-telegram-commands":
        set_telegram_bot_commands(cfg, dry_run=args.dry_run)
        print("Telegram 命令菜单已注册。")
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
            "user_recharge_alerts_enabled": cfg.user_recharge_alerts_enabled,
            "user_recharges_initialized": mon.state.get("user_recharges_initialized"),
            "last_error_id": mon.state.get("last_error_id"),
        }, ensure_ascii=False, indent=2))
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
