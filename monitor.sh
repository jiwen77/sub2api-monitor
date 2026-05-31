#!/usr/bin/env bash
set -Eeuo pipefail

APP="sub2api-monitor"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/sub2api-monitor"
UPDATE_REPO_URL="${UPDATE_REPO_URL:-https://github.com/jiwen77/sub2api-monitor.git}"
UPDATE_REF="${UPDATE_REF:-main}"
CONFIG_DIR="/etc/sub2api-monitor"
CONFIG_FILE="$CONFIG_DIR/config.env"
STATE_DIR="/var/lib/sub2api-monitor"
LOG_DIR="/var/log/sub2api-monitor"
SERVICE_FILE="/etc/systemd/system/sub2api-monitor.service"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"

red='\033[31m'; green='\033[32m'; yellow='\033[33m'; blue='\033[34m'; cyan='\033[96m'; reset='\033[0m'

need_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo -e "${yellow}需要 root 权限，尝试 sudo...${reset}"
    exec sudo -E bash "$0" "$@"
  fi
}

pause() { read -r -p "按 Enter 继续..." _ || true; }

set_env_value() {
  local key="$1" value="$2" file="$3"
  mkdir -p "$(dirname "$file")"
  touch "$file"
  chmod 600 "$file"
  local escaped
  escaped=$(printf '%s' "$value" | sed -e 's/[\\&|]/\\&/g')
  if grep -qE "^#?${key}=" "$file"; then
    sed -i -E "s|^#?${key}=.*|${key}=${escaped}|" "$file"
  else
    printf '\n%s=%s\n' "$key" "$value" >> "$file"
  fi
}

same_file() {
  local src="$1" dst="$2"
  [[ -e "$src" && -e "$dst" ]] || return 1
  [[ "$(readlink -f "$src")" == "$(readlink -f "$dst")" ]]
}

install_or_chmod() {
  local mode="$1" src="$2" dst="$3"
  if same_file "$src" "$dst"; then
    chmod "$mode" "$dst"
  else
    install -m "$mode" "$src" "$dst"
  fi
}

install_files() {
  need_root "$@"
  echo -e "${blue}安装/更新本地项目文件到 $INSTALL_DIR${reset}"
  mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$STATE_DIR" "$LOG_DIR" "$INSTALL_DIR/systemd"
  install_or_chmod 0755 "$SRC_DIR/sub2api_monitor.py" "$INSTALL_DIR/sub2api_monitor.py"
  install_or_chmod 0755 "$SRC_DIR/monitor.sh" "$INSTALL_DIR/monitor.sh"
  install_or_chmod 0644 "$SRC_DIR/README.md" "$INSTALL_DIR/README.md"
  install_or_chmod 0644 "$SRC_DIR/systemd/sub2api-monitor.service" "$INSTALL_DIR/systemd/sub2api-monitor.service"
  if [[ ! -f "$CONFIG_FILE" ]]; then
    install -m 0600 "$SRC_DIR/config.env.example" "$CONFIG_FILE"
    echo -e "${green}已创建配置：$CONFIG_FILE${reset}"
  else
    echo -e "${green}保留已有配置：$CONFIG_FILE${reset}"
  fi
  chmod 700 "$STATE_DIR" "$LOG_DIR"
  echo -e "${green}安装完成。${reset}"
}

update_from_github() {
  need_root "$@"
  echo -e "${blue}从 GitHub 拉取并安装 $APP${reset}"
  echo "仓库: $UPDATE_REPO_URL"
  echo "分支/标签: $UPDATE_REF"

  if ! command -v git >/dev/null 2>&1; then
    echo -e "${red}未找到 git，请先安装 git 后重试。${reset}"
    return 1
  fi

  local tmp
  tmp=$(mktemp -d /tmp/sub2api-monitor-update.XXXXXX)
  trap 'rm -rf "$tmp"' RETURN

  git clone --depth 1 --branch "$UPDATE_REF" "$UPDATE_REPO_URL" "$tmp/repo"

  if [[ ! -f "$tmp/repo/sub2api_monitor.py" || ! -f "$tmp/repo/monitor.sh" ]]; then
    echo -e "${red}仓库内容不完整，未安装。${reset}"
    return 1
  fi

  mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$STATE_DIR" "$LOG_DIR"
  rsync -a --delete \
    --exclude '.git/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    "$tmp/repo/" "$INSTALL_DIR/"
  chmod +x "$INSTALL_DIR/sub2api_monitor.py" "$INSTALL_DIR/monitor.sh"

  if [[ ! -f "$CONFIG_FILE" ]]; then
    install -m 0600 "$INSTALL_DIR/config.env.example" "$CONFIG_FILE"
    echo -e "${green}已创建配置：$CONFIG_FILE${reset}"
  else
    echo -e "${green}保留已有配置：$CONFIG_FILE${reset}"
  fi

  install -m 0644 "$INSTALL_DIR/systemd/sub2api-monitor.service" "$SERVICE_FILE"
  systemctl daemon-reload || true
  chmod 700 "$STATE_DIR" "$LOG_DIR"
  "$PYTHON_BIN" -m py_compile "$INSTALL_DIR/sub2api_monitor.py"

  if systemctl is-active --quiet sub2api-monitor.service; then
    systemctl restart sub2api-monitor.service
    echo -e "${green}已更新并重启 sub2api-monitor.service。${reset}"
  else
    echo -e "${green}已更新项目文件；服务当前未运行。${reset}"
  fi
}

configure_tg() {
  need_root "$@"
  [[ -f "$CONFIG_FILE" ]] || install_files
  echo -e "${cyan}配置 Telegram Bot${reset}"
  read -r -p "Bot Token: " token
  read -r -p "Chat ID: " chat
  set_env_value "TELEGRAM_BOT_TOKEN" "$token" "$CONFIG_FILE"
  set_env_value "TELEGRAM_CHAT_ID" "$chat" "$CONFIG_FILE"
  read -r -p "是否在启动时发送账号基线？[Y/n] " startup
  startup=${startup:-Y}
  if [[ "$startup" =~ ^[Nn]$ ]]; then
    set_env_value "SEND_STARTUP_SUMMARY" "false" "$CONFIG_FILE"
  else
    set_env_value "SEND_STARTUP_SUMMARY" "true" "$CONFIG_FILE"
  fi
  echo -e "${green}Telegram 配置已保存。${reset}"
}


get_env_value() {
  local key="$1" default="${2:-}" file="${3:-$CONFIG_FILE}"
  if [[ -f "$file" ]] && grep -qE "^${key}=" "$file"; then
    grep -E "^${key}=" "$file" | tail -n1 | sed -E "s|^${key}=||"
  else
    printf '%s' "$default"
  fi
}

save_env_value() {
  local key="$1" value="$2" current
  current=$(get_env_value "$key" "")
  if [[ "$value" != "$current" ]]; then
    set_env_value "$key" "$value" "$CONFIG_FILE"
    CONFIG_CHANGED=1
    echo -e "${green}已保存：$key=$value${reset}"
  else
    echo -e "${yellow}未变化：$key${reset}"
  fi
}

prompt_text() {
  local key="$1" label="$2" default="${3:-}" current value display
  current=$(get_env_value "$key" "$default")
  display=${current:-空}
  read -r -p "$label [$display]（回车保留，输入 - 清空）: " value
  [[ -z "$value" ]] && return 0
  [[ "$value" == "-" ]] && value=""
  save_env_value "$key" "$value"
}

prompt_bool() {
  local key="$1" label="$2" default="${3:-true}" current value normalized
  current=$(get_env_value "$key" "$default")
  while true; do
    read -r -p "$label [$current]（true/false，回车保留）: " value
    [[ -z "$value" ]] && return 0
    case "$value" in
      true|TRUE|True|yes|YES|Yes|y|Y|1) normalized="true" ;;
      false|FALSE|False|no|NO|No|n|N|0) normalized="false" ;;
      *) echo -e "${red}请输入 true 或 false。${reset}"; continue ;;
    esac
    save_env_value "$key" "$normalized"
    return 0
  done
}

prompt_int() {
  local key="$1" label="$2" default="$3" min="$4" max="$5" current value
  current=$(get_env_value "$key" "$default")
  while true; do
    read -r -p "$label [$current]（$min-$max，回车保留）: " value
    [[ -z "$value" ]] && return 0
    if ! [[ "$value" =~ ^[0-9]+$ ]]; then
      echo -e "${red}请输入整数。${reset}"
      continue
    fi
    if (( value < min || value > max )); then
      echo -e "${red}请输入 $min 到 $max 之间的数字。${reset}"
      continue
    fi
    save_env_value "$key" "$value"
    return 0
  done
}

print_config_summary() {
  [[ -f "$CONFIG_FILE" ]] || install_files
  echo -e "${cyan}当前常用配置${reset}"
  echo "监控采样频率: $(get_env_value POLL_INTERVAL_SECONDS 60) 秒"
  echo "时区: $(get_env_value TZ Asia/Shanghai)"
  echo "启动时发送账号基线: $(get_env_value SEND_STARTUP_SUMMARY true)"
  echo "账号信息脱敏: $(get_env_value REDACT_IDENTIFIERS true)"
  echo "每条消息最多展开: $(get_env_value DETAIL_LIMIT 12) 条"
  echo "TG 命令开关: $(get_env_value TELEGRAM_COMMANDS_ENABLED true)"
  echo "TG 命令检查频率: $(get_env_value TELEGRAM_COMMAND_POLL_INTERVAL_SECONDS 5) 秒"
  echo "TG 命令允许 Chat IDs: $(get_env_value TELEGRAM_ALLOWED_CHAT_IDS '')"
  echo "启动时丢弃历史 TG 命令: $(get_env_value TELEGRAM_DROP_PENDING_UPDATES true)"
  echo "上游错误回看窗口: $(get_env_value ERROR_LOOKBACK_MINUTES 30) 分钟"
  echo "上游错误读取上限: $(get_env_value ERROR_LIMIT_PER_POLL 500) 条/轮"
  echo "同类错误冷却时间: $(get_env_value ERROR_COOLDOWN_SECONDS 600) 秒"
  echo "告警状态码: $(get_env_value UPSTREAM_ALLOWED_STATUS_CODES '429,500-599')"
  echo "首次启动告警历史错误: $(get_env_value ALERT_EXISTING_ERRORS_ON_FIRST_RUN false)"
  local daily_hour daily_minute
  daily_hour=$(get_env_value DAILY_REPORT_HOUR 0)
  daily_minute=$(get_env_value DAILY_REPORT_MINUTE 0)
  if [[ "$daily_hour" =~ ^[0-9]+$ && "$daily_minute" =~ ^[0-9]+$ ]]; then
    printf '日报时间: %02d:%02d\n' "$daily_hour" "$daily_minute"
  else
    echo "日报时间: ${daily_hour}:${daily_minute}"
  fi
  echo "错过日报后补发: $(get_env_value DAILY_CATCHUP true)"
  echo "Sub2API 目录: $(get_env_value SUB2API_DIR /opt/sub2api)"
  echo "Postgres 容器: $(get_env_value POSTGRES_CONTAINER sub2api-postgres)"
  echo "数据库查询超时: $(get_env_value PSQL_TIMEOUT_SECONDS 20) 秒"
}

restart_service_if_changed() {
  if [[ "${CONFIG_CHANGED:-0}" != "1" ]]; then
    return 0
  fi
  echo -e "${green}配置已保存到 $CONFIG_FILE${reset}"
  if systemctl is-active --quiet sub2api-monitor.service; then
    local ok
    read -r -p "后台监控正在运行，是否立即重启让配置生效？[Y/n] " ok
    ok=${ok:-Y}
    if [[ "$ok" =~ ^[Nn]$ ]]; then
      echo -e "${yellow}已保存但尚未重启；可稍后选 10 生效。${reset}"
    else
      systemctl restart sub2api-monitor.service
      echo -e "${green}后台监控已重启，新配置已生效。${reset}"
    fi
  else
    echo -e "${yellow}后台监控未运行；下次选 10 启动时生效。${reset}"
  fi
}

configure_runtime_options() {
  need_root "$@"
  [[ -f "$CONFIG_FILE" ]] || install_files
  CONFIG_CHANGED=0
  while true; do
    echo
    echo -e "${cyan}交互式修改配置项${reset}"
    echo " 1) 查看当前常用配置"
    echo " 2) 监控频率/时区"
    echo " 3) Telegram 命令设置（/status 等）"
    echo " 4) 账号告警显示设置"
    echo " 5) 上游错误告警设置"
    echo " 6) 日报时间设置"
    echo " 7) Sub2API / 数据库连接设置"
    echo " 0) 返回主菜单"
    echo
    read -r -p "请选择要修改的配置: " subchoice
    case "$subchoice" in
      1) print_config_summary; pause ;;
      2)
        prompt_int POLL_INTERVAL_SECONDS "监控采样频率，越小越及时但查询更频繁，建议 30-120 秒" 60 5 3600
        prompt_text TZ "时区" Asia/Shanghai
        ;;
      3)
        prompt_bool TELEGRAM_COMMANDS_ENABLED "是否允许在 TG 里发 /status、/daily 等命令" true
        prompt_int TELEGRAM_COMMAND_POLL_INTERVAL_SECONDS "TG 命令检查频率，影响 /status 响应速度" 5 1 300
        prompt_text TELEGRAM_ALLOWED_CHAT_IDS "允许使用 TG 命令的 Chat ID 列表，逗号分隔；留空=只允许通知 Chat ID" ""
        prompt_bool TELEGRAM_DROP_PENDING_UPDATES "启动时是否丢弃历史 TG 命令，避免旧命令被补处理" true
        ;;
      4)
        prompt_bool SEND_STARTUP_SUMMARY "监控启动/重启时是否发送账号基线" true
        prompt_bool REDACT_IDENTIFIERS "TG 消息里是否隐藏账号邮箱/名称" true
        prompt_int DETAIL_LIMIT "每条消息最多展开多少个账号/错误" 12 1 100
        ;;
      5)
        prompt_int ERROR_LOOKBACK_MINUTES "每轮检查最近多少分钟内的上游错误" 30 1 1440
        prompt_int ERROR_LIMIT_PER_POLL "每轮最多读取多少条错误日志" 500 1 5000
        prompt_int ERROR_COOLDOWN_SECONDS "同类错误冷却时间，避免刷屏；0 表示不冷却" 600 0 86400
        prompt_text UPSTREAM_ALLOWED_STATUS_CODES "哪些上游 HTTP 状态码会告警，例如 429,500-599" "429,500-599"
        prompt_bool ALERT_EXISTING_ERRORS_ON_FIRST_RUN "首次启动是否告警历史错误；通常建议 false" false
        ;;
      6)
        prompt_int DAILY_REPORT_HOUR "日报发送小时" 0 0 23
        prompt_int DAILY_REPORT_MINUTE "日报发送分钟" 0 0 59
        prompt_bool DAILY_CATCHUP "如果程序错过日报时间，恢复后是否补发" true
        ;;
      7)
        prompt_text SUB2API_DIR "Sub2API 安装目录" /opt/sub2api
        prompt_text POSTGRES_CONTAINER "Postgres 容器名" sub2api-postgres
        prompt_int PSQL_TIMEOUT_SECONDS "数据库查询超时秒数" 20 3 120
        ;;
      0) restart_service_if_changed; return 0 ;;
      *) echo -e "${red}无效选择${reset}" ;;
    esac
  done
}

run_py() {
  local cmd=("$PYTHON_BIN" "$INSTALL_DIR/sub2api_monitor.py" --config "$CONFIG_FILE" "$@")
  if [[ ! -x "$INSTALL_DIR/sub2api_monitor.py" ]]; then
    cmd=("$PYTHON_BIN" "$SRC_DIR/sub2api_monitor.py" --config "$CONFIG_FILE" "$@")
  fi
  "${cmd[@]}"
}

install_service() {
  need_root "$@"
  install_files
  install -m 0644 "$INSTALL_DIR/systemd/sub2api-monitor.service" "$SERVICE_FILE"
  systemctl daemon-reload
  systemctl enable sub2api-monitor.service
  systemctl restart sub2api-monitor.service
  echo -e "${green}后台监控服务已启动/重启。${reset}"
  systemctl --no-pager --full status sub2api-monitor.service || true
}

stop_service() {
  need_root "$@"
  systemctl disable --now sub2api-monitor.service || true
  echo -e "${green}后台监控服务已停止并禁用。${reset}"
}

show_status() {
  systemctl --no-pager --full status sub2api-monitor.service || true
  echo
  journalctl -u sub2api-monitor.service -n 80 --no-pager || true
}

uninstall_monitor() {
  need_root "$@"
  read -r -p "确认停止服务并删除 $INSTALL_DIR 和 systemd unit？配置/状态默认保留。[y/N] " ok
  [[ "$ok" =~ ^[Yy]$ ]] || return 0
  systemctl disable --now sub2api-monitor.service || true
  rm -f "$SERVICE_FILE"
  systemctl daemon-reload || true
  rm -rf "$INSTALL_DIR"
  echo -e "${green}已卸载程序文件。配置仍在 $CONFIG_FILE，状态在 $STATE_DIR。${reset}"
}

edit_config() {
  need_root "$@"
  [[ -f "$CONFIG_FILE" ]] || install_files
  "${EDITOR:-nano}" "$CONFIG_FILE"
}

menu() {
  while true; do
    if [[ -t 1 && -n "${TERM:-}" ]]; then
      clear || true
    fi
    echo -e "${cyan}=================================================${reset}"
    echo -e "${cyan}        sub2api-monitor 交互管理脚本${reset}"
    echo -e "${cyan}=================================================${reset}"
    echo "安装目录: $INSTALL_DIR"
    echo "配置文件: $CONFIG_FILE"
    echo
    echo " 1) 安装/更新程序（从 GitHub 拉取）"
    echo " 2) 配置 Telegram 通知"
    echo " 3) 测试 Telegram 通知"
    echo " 4) 查看账号状态（只显示，不发 TG）"
    echo " 5) 发送账号状态到 TG（立即发送）"
    echo " 6) 手动检查告警（有变化/错误才发 TG）"
    echo " 7) 预览日报（只显示，不发 TG）"
    echo " 8) 发送日报到 TG（立即发送）"
    echo " 9) 临时运行监控（关窗口会停止）"
    echo "10) 后台启动/重启监控（推荐）"
    echo "11) 查看后台监控状态/日志"
    echo "12) 停止后台监控并关闭自启"
    echo "13) 交互式修改配置项"
    echo "14) 手动编辑配置文件（nano）"
    echo "15) 卸载程序文件（保留配置）"
    echo " 0) 退出"
    echo
    read -r -p "请选择: " choice
    case "$choice" in
      1) update_from_github; pause ;;
      2) configure_tg; pause ;;
      3) run_py test-telegram; pause ;;
      4) run_py account-summary; pause ;;
      5) run_py account-summary --notify; pause ;;
      6) run_py run-once --notify; pause ;;
      7) run_py daily; pause ;;
      8) run_py daily --notify; pause ;;
      9) run_py daemon ;;
      10) install_service; pause ;;
      11) show_status; pause ;;
      12) stop_service; pause ;;
      13) configure_runtime_options; pause ;;
      14) edit_config; pause ;;
      15) uninstall_monitor; pause ;;
      0) exit 0 ;;
      *) echo -e "${red}无效选择${reset}"; sleep 1 ;;
    esac
  done
}

case "${1:-}" in
  --update|-u)
    shift || true
    update_from_github "$@"
    exit $?
    ;;
  --install-local)
    shift || true
    install_files "$@"
    exit $?
    ;;
  --help|-h)
    echo "Usage: $0 [--update|-u|--install-local]"
    echo "Without arguments, opens the interactive menu."
    exit 0
    ;;
esac

menu "$@"
