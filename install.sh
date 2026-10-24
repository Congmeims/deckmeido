#!/usr/bin/env bash
# DeckMeido 一键安装 / 升级 / 卸载
#
#   bash install.sh              安装或升级到当前目录（可重复执行）
#   bash install.sh install       同上
#   bash install.sh upgrade        升级代码与依赖，保留配置
#   bash install.sh uninstall     卸载（默认保留 ~/.config/deckmeido 配置与工作区）
#   bash install.sh uninstall --purge   连配置一起删
#
# 环境变量：
#   DECKMEIDO_DIR   安装目录，默认 ~/deckmeido
#   DECKMEIDO_PORT  服务端口，默认 18765
#
# 注意：不要用 sudo 执行。脚本会把程序装到当前用户家目录、注册用户级服务，
#       用 root 装会装到 /root 并注册到 root 名下，桌面会话里起不来。
set -euo pipefail

APP=deckmeido
INSTALL_DIR="${DECKMEIDO_DIR:-$HOME/$APP}"
PORT="${DECKMEIDO_PORT:-18765}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/$APP"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="deckmeido.service"
DESKTOP_DIR="$HOME/.local/share/applications"
# 真正的「桌面」目录：可能是 Desktop 也可能是 桌面，取 xdg 配置，没配置就默认 Desktop
DESKTOP_FOLDER="$(command -v xdg-user-dir >/dev/null 2>&1 && xdg-user-dir DESKTOP 2>/dev/null || true)"
[ -n "$DESKTOP_FOLDER" ] && [ "$DESKTOP_FOLDER" != "$HOME" ] || DESKTOP_FOLDER="$HOME/Desktop"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------- 输出 ----------
if [ -t 1 ]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_DIM=$'\033[2m'; C_END=$'\033[0m'
else
  C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_END=""
fi
info() { printf '%s==>%s %s\n' "$C_OK" "$C_END" "$*"; }
warn() { printf '%s[!]%s %s\n' "$C_WARN" "$C_END" "$*"; }
die()  { printf '%s[x]%s %s\n' "$C_ERR" "$C_END" "$*" >&2; exit 1; }
dim()  { printf '%s    %s%s\n' "$C_DIM" "$*" "$C_END"; }

need_cmd() { command -v "$1" >/dev/null 2>&1; }

# ---------- 前置检查 ----------
check_user() {
  if [ "$(id -u)" = "0" ]; then
    die "不要用 sudo / root 运行。请直接执行：bash install.sh（会自动装到 $HOME/$APP）"
  fi
}

check_prereq() {
  check_user
  need_cmd python3 || die "找不到 python3，请先在系统设置里开启开发者模式并安装 Python"
  python3 - <<'PY' || die "需要 Python 3.11 或更高版本"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
  if ! python3 -c "import venv, ensurepip" >/dev/null 2>&1; then
    die "缺少 python3-venv，请先安装（SteamOS: sudo steamos-readonly disable && sudo pacman -S python）"
  fi
  need_cmd systemctl || warn "没有 systemctl，将不会注册开机自启服务"
}

system_notes() {
  # 只提示不自动装：装系统包需要 sudo，交给用户确认更稳妥
  local miss=()
  need_cmd espeak-ng || miss+=("espeak-ng（语音播报）")
  need_cmd kdotool   || miss+=("kdotool（按窗口名聚焦，AUR）")
  need_cmd ydotool   || miss+=("ydotool（Wayland 下模拟点击/输入，AUR）")
  need_cmd spectacle || miss+=("spectacle（KWin 截图，一般自带）")
  if [ ${#miss[@]} -gt 0 ]; then
    warn "以下可选依赖没装，相关功能会受限："
    for m in "${miss[@]}"; do dim "$m"; done
    dim "SteamOS 装 AUR 包：先设置 → 开发者模式，再用 pacman/paru 安装"
  fi
  # ydotool 装了但 ydotoold 没跑，点击/输入会直接报 socket 错误，这里提前点出来
  if need_cmd ydotool && ! pgrep -x ydotoold >/dev/null 2>&1; then
    warn "ydotool 已装但 ydotoold 守护进程没在运行，模拟点击/输入会失败"
    dim "临时启动：ydotoold >/dev/null 2>&1 &"
  fi
}

# ---------- 代码同步 ----------
# 从 tar.gz/解压目录安装时，把需要的文件复制进安装目录（不覆盖已有配置与会话）
sync_files() {
  if [ "$HERE" = "$(cd "$INSTALL_DIR" 2>/dev/null && pwd -P)" ]; then
    info "已在使用目录 $INSTALL_DIR"
    return
  fi
  [ -f "$HERE/install.sh" ] || die "当前目录不像 DeckMeido 源码目录（缺 install.sh）"
  info "同步代码到 $INSTALL_DIR"
  mkdir -p "$INSTALL_DIR"
  local items=(server client assets deploy tools requirements.txt README.md install.sh)
  for it in "${items[@]}"; do
    [ -e "$HERE/$it" ] || continue
    if [ -d "$HERE/$it" ]; then
      rsync -a --exclude '__pycache__' --exclude 'config.toml' --exclude 'sessions' \
            --exclude 'uploads' --exclude 'shots' --exclude '*.log' \
            "$HERE/$it/" "$INSTALL_DIR/$it/" 2>/dev/null ||
        cp -r "$HERE/$it/." "$INSTALL_DIR/$it/"
    else
      cp -f "$HERE/$it" "$INSTALL_DIR/$it"
    fi
  done
  chmod +x "$INSTALL_DIR/install.sh" 2>/dev/null || true
}

venv_install() {
  local py="$INSTALL_DIR/.venv/bin/python"
  if [ ! -x "$py" ]; then
    info "创建虚拟环境 $INSTALL_DIR/.venv"
    python3 -m venv "$INSTALL_DIR/.venv"
  fi
  info "安装 Python 依赖（首次较慢，PySide6 体积较大）"
  "$py" -m pip install --upgrade pip -q
  "$py" -m pip install -r "$INSTALL_DIR/requirements.txt" -q
}

# 首次安装生成 config.toml；已有则原样保留
prepare_config() {
  local cfg="$INSTALL_DIR/server/config.toml"
  if [ -f "$cfg" ]; then
    info "保留已有配置 $cfg"
  else
    cp "$INSTALL_DIR/server/config.example.toml" "$cfg"
    chmod 600 "$cfg"
    info "生成配置 $cfg"
  fi
}

install_service() {
  need_cmd systemctl || return 0
  mkdir -p "$UNIT_DIR"
  sed "s|%h/deckmeido|$INSTALL_DIR|g; s|--port 18765|--port $PORT|g" \
    "$INSTALL_DIR/deploy/$UNIT" > "$UNIT_DIR/$UNIT"
  systemctl --user daemon-reload
  # 必须先 enable 再 restart：服务已在跑时 `enable --now` 不会重新拉起进程，
  # 升级后还在跑旧代码
  systemctl --user enable "$UNIT" >/dev/null 2>&1 || true
  systemctl --user restart "$UNIT"
  dim "用户服务已启用：systemctl --user status $UNIT"
}

install_desktop() {
  mkdir -p "$DESKTOP_DIR"
  sed "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
    "$INSTALL_DIR/deploy/DeckMeido.desktop" > "$DESKTOP_DIR/DeckMeido.desktop"
  chmod +x "$DESKTOP_DIR/DeckMeido.desktop"
  need_cmd update-desktop-database && update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true

  # 同时放一份到桌面
  if [ -d "$DESKTOP_FOLDER" ]; then
    cp "$DESKTOP_DIR/DeckMeido.desktop" "$DESKTOP_FOLDER/DeckMeido.desktop"
    chmod +x "$DESKTOP_FOLDER/DeckMeido.desktop"
    # GNOME 系桌面需要标记为可信，否则双击会提示不安全
    need_cmd gio && gio set "$DESKTOP_FOLDER/DeckMeido.desktop" metadata::trusted true >/dev/null 2>&1 || true
  fi

  dim "快捷方式已放到应用菜单和桌面（游戏模式下可加到非 Steam 游戏）"
}

read_token() {
  local cfg="$INSTALL_DIR/server/config.toml"
  [ -f "$cfg" ] || return 0
  grep -oP '^token\s*=\s*"\K[^"]*' "$cfg" 2>/dev/null | head -n1
}

wait_ready() {
  local url="http://127.0.0.1:$PORT/"
  for _ in $(seq 1 40); do
    if curl -fsS -o /dev/null "$url" 2>/dev/null; then return 0; fi
    sleep 0.5
  done
  return 1
}

lan_ip() {
  # 优先挑私有网段的物理网卡：开了 Clash 等代理后，默认路由出口会是
  # 198.18.x.x 的虚拟地址，别的设备连不上
  "$INSTALL_DIR/.venv/bin/python" - <<'PY' 2>/dev/null || true
import socket

try:
    import psutil
except ImportError:
    psutil = None

best = None
if psutil:
    for name, addrs in psutil.net_if_addrs().items():
        low = name.lower()
        if any(k in low for k in ("meta", "tun", "tap", "docker", "veth",
                                  "br-", "virbr", "vmnet", "wg", "zt")):
            continue
        for a in addrs:
            if a.family != socket.AF_INET or not a.address:
                continue
            ip = a.address
            if ip.startswith(("192.168.", "10.")) or ip.startswith("172."):
                rank = 0 if low.startswith(("wlan", "wlp", "wl")) else 1
                if best is None or rank < best[0]:
                    best = (rank, ip)

if best:
    print(best[1])
else:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        print(s.getsockname()[0])
    finally:
        s.close()
PY
}

print_done() {
  local ip token
  ip=$(lan_ip)
  [ -n "$ip" ] || ip=$(hostname -I 2>/dev/null | awk '{print $1}')
  [ -n "$ip" ] || ip="<deck-ip>"
  token=$(read_token)
  echo
  info "DeckMeido 已就绪"
  dim "本机控制台:  http://127.0.0.1:$PORT/?token=$token"
  dim "局域网访问:  http://$ip:$PORT/?token=$token"
  dim "访问令牌:    $token   （可在网页设置页随机生成或自定义）"
  dim "配置文件:    $INSTALL_DIR/server/config.toml"
  echo
  dim "下一步：打开上面的地址，首次会弹出设置页，填 API 地址与 Key 即可对话"
}

# ---------- 子命令 ----------
do_install() {
  check_prereq
  info "安装目录：$INSTALL_DIR"
  sync_files
  venv_install
  prepare_config
  install_service
  install_desktop
  system_notes
  if need_cmd curl && wait_ready; then
    print_done
  else
    warn "服务还没就绪，查看日志：journalctl --user -u $UNIT -n 50"
  fi
}

do_upgrade() {
  [ -d "$INSTALL_DIR" ] || die "$INSTALL_DIR 不存在，请先执行安装"
  check_prereq
  sync_files
  venv_install
  install_service
  install_desktop
  if need_cmd curl && wait_ready; then
    print_done
  else
    warn "升级完成但服务未就绪，查看日志：journalctl --user -u $UNIT -n 50"
  fi
}

do_uninstall() {
  local purge=0
  [ "${1:-}" = "--purge" ] && purge=1
  check_user
  if need_cmd systemctl; then
    info "停止并移除用户服务"
    systemctl --user disable --now "$UNIT" >/dev/null 2>&1 || true
    rm -f "$UNIT_DIR/$UNIT"
    systemctl --user daemon-reload || true
  fi
  rm -f "$DESKTOP_DIR/DeckMeido.desktop"
  rm -f "$DESKTOP_FOLDER/DeckMeido.desktop"
  if [ "$purge" = 1 ]; then
    warn "删除配置与工作区（不可恢复）"
    rm -rf "$CONFIG_DIR"
    rm -rf "${DECKMEIDO_WORKSPACE:-$HOME/deckmeido_workspace}"
  fi
  # 只删本仓库自带的运行产物，避免误删用户数据
  rm -rf "$INSTALL_DIR/server/sessions" "$INSTALL_DIR/server/static/shots" \
         "$INSTALL_DIR/server/uploads" "$INSTALL_DIR/.venv"
  info "卸载完成"
  dim "代码与配置仍保留在 $INSTALL_DIR；完全删除：rm -rf $INSTALL_DIR"
}

case "${1:-install}" in
  install|"")   do_install ;;
  upgrade|update) do_upgrade ;;
  uninstall)    do_uninstall "${2:-}" ;;
  -h|--help|help)
    grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -n 16 ;;
  *) die "未知命令：$1（可用：install / upgrade / uninstall）" ;;
esac
