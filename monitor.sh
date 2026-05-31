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
  escaped=$(printf '%s' "$value" | sed -e 's/[\\&]/\\&/g')
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
  systemctl enable --now sub2api-monitor.service
  echo -e "${green}systemd 服务已启动。${reset}"
  systemctl --no-pager --full status sub2api-monitor.service || true
}

stop_service() {
  need_root "$@"
  systemctl disable --now sub2api-monitor.service || true
  echo -e "${green}服务已停止并禁用。${reset}"
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
    echo " 1) 从 GitHub 安装/更新项目文件"
    echo " 2) 配置 Telegram"
    echo " 3) 发送 Telegram 测试"
    echo " 4) 查看当前账号状态（不通知）"
    echo " 5) 强制推送当前账号状态快照"
    echo " 6) 手动巡检一次（仅变化/上游错误才告警）"
    echo " 7) 生成日报（不通知）"
    echo " 8) 立即发送日报"
    echo " 9) 前台运行 daemon"
    echo "10) 安装并启动 systemd 服务"
    echo "11) 查看服务状态/日志"
    echo "12) 停止并禁用服务"
    echo "13) 编辑配置"
    echo "14) 卸载程序文件"
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
      13) edit_config; pause ;;
      14) uninstall_monitor; pause ;;
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
