#!/usr/bin/env bash
# DeckMeido 一键安装 / 升级 / 卸载
#
#   bash install.sh              安装或升级到当前目录（可重复执行）
#   bash install.sh install       同上
#   bash install.sh install --deps      顺带用 sudo 安装控制类插件（不再询问）
#   bash install.sh upgrade        升级代码与依赖，保留配置
#   bash install.sh upgrade --deps      升级后顺带检查并安装控制类插件
#   bash install.sh uninstall     卸载（默认保留 ~/.config/deckmeido 配置与工作区）
#   bash install.sh uninstall --purge   连配置一起删
#
# 控制类插件（ydotool / kdotool / espeak-ng）是系统包，装它们需要 root：
# 默认只在结尾提示并询问一次（y/N），回答 y 才走 sudo；无终端时不会卡住。
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
  local miss=()
  need_cmd espeak-ng || miss+=("espeak-ng（语音播报回复）")
  need_cmd kdotool   || miss+=("kdotool（按窗口名切换/聚焦窗口）")
  need_cmd ydotool   || miss+=("ydotool（模拟鼠标点击、键盘输入）")
  need_cmd spectacle || miss+=("spectacle（KWin 截图，一般自带）")
  if [ ${#miss[@]} -gt 0 ]; then
    warn "以下系统控件没装，相关功能会受限："
    for m in "${miss[@]}"; do dim "$m"; done
    dim "想自动安装：bash install.sh install --deps"
  fi
  # ydotool 装了但 ydotoold 没跑，点击/输入会直接报 socket 错误，这里提前点出来
  if need_cmd ydotool && ! pgrep -x ydotoold >/dev/null 2>&1; then
    warn "ydotool 已装但 ydotoold 守护进程没在运行，模拟点击/输入会失败"
    dim "临时启动：ydotoold >/dev/null 2>&1 &"
  fi
}

# ---------- 可选系统依赖 ----------
# 这些是「替你在桌面上动手」的核心控件，装它们必须 root。
# 默认在安装结尾说明用途并询问一次，或加 --deps 直接装。
DEPS_PACMAN=(espeak-ng ydotool)
DEPS_AUR=(kdotool)

deps_intro() {
  # 安装前把每个控件是干什么的说清楚，用户才知道为什么要给 sudo
  local miss_pacman=() miss_aur=() miss_other=()
  need_cmd espeak-ng || miss_pacman+=("espeak-ng")
  need_cmd ydotool   || miss_pacman+=("ydotool")
  need_cmd kdotool   || miss_aur+=("kdotool")
  need_cmd spectacle || miss_other+=("spectacle")

  echo
  info "关于控制类系统控件（推荐安装）"
  dim "DeckMeido 的「帮你在桌面上操作」能力依赖下面这些系统包；"
  dim "不装也能聊天、截图、执行命令，但没法替你点鼠标、敲键盘、念出回复。"
  echo
  printf '  %sydotool%s   模拟鼠标点击 / 键盘输入（搭配 ydotoold 守护进程）\n' "$C_OK" "$C_END"
  printf '  %skdotool%s   按窗口标题查找并聚焦窗口，配合 ydotool 点击正确的位置\n' "$C_OK" "$C_END"
  printf '  %sespeak-ng%s 把回复念出来（语音播报）\n' "$C_OK" "$C_END"
  printf '  %sspectacle%s KDE 截图工具，用于「看看当前屏幕」\n' "$C_OK" "$C_END"
  echo
  [ ${#miss_pacman[@]} -gt 0 ] && dim "待安装（pacman）：${miss_pacman[*]}"
  [ ${#miss_aur[@]}    -gt 0 ] && dim "待安装（AUR）：${miss_aur[*]}（需要 paru 或 yay）"
  [ ${#miss_other[@]}  -gt 0 ] && dim "未找到：${miss_other[*]}（KDE 一般自带，可忽略）"
  dim "安装过程会用 sudo 关闭 SteamOS 根分区只读保护并安装系统包，不会改动你的游戏与个人文件。"
}

ask_yes() {
  # 无 tty（比如从客户端里跑）时默认不装，避免卡住
  [ -t 0 ] || return 1
  local ans
  printf '%s?%s %s [y/N] ' "$C_WARN" "$C_END" "$1"
  read -r ans
  case "$ans" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

sudo_warmup() {
  need_cmd sudo || { warn "没有 sudo，跳过系统依赖安装"; return 1; }
  if ! sudo -n true 2>/dev/null; then
    info "安装系统依赖需要 root 权限，接下来会提示输入密码"
  fi
  sudo -v || { warn "sudo 授权失败，跳过系统依赖安装"; return 1; }
}

install_pacman_deps() {
  local todo=()
  for p in "${DEPS_PACMAN[@]}"; do
    need_cmd "$p" || todo+=("$p")
  done
  [ ${#todo[@]} -gt 0 ] || return 0
  info "用 pacman 安装：${todo[*]}"
  # SteamOS 的根分区是只读的，装包前要先关掉只读保护
  if [ -f /etc/steamos-readonly.conf ] || need_cmd steamos-readonly; then
    sudo steamos-readonly disable 2>/dev/null || true
  fi
  # --needed：已装的不会重装；-Sy 而不 -Syu，避免顺带滚系统
  if won=$(sudo pacman -Sy --needed --noconfirm "${todo[@]}" 2>&1); then
    info "pacman 安装完成"
  else
    warn "pacman 安装失败："
    dim "$won"
    dim "SteamOS 换过 keyring 的话先执行：sudo pacman-key --init && sudo pacman-key --populate archlinux holo"
  fi
}

install_aur_deps() {
  local todo=() helper
  for p in "${DEPS_AUR[@]}"; do
    need_cmd "$p" || todo+=("$p")
  done
  [ ${#todo[@]} -gt 0 ] || return 0
  helper=""
  for h in paru yay; do
    need_cmd "$h" && { helper="$h"; break; }
  done
  if [ -z "$helper" ]; then
    warn "这些包在 AUR 里，但没找到 paru / yay，跳过：${todo[*]}"
    dim "手动装法：git clone https://aur.archlinux.org/<包名>.git && cd <包名> && makepkg -si"
    return 0
  fi
  info "用 $helper 安装：${todo[*]}"
  if won=$("$helper" -S --needed --noconfirm "${todo[@]}" 2>&1); then
    info "$helper 安装完成"
  else
    warn "$helper 安装失败："
    dim "$won"
  fi
}

# ydotoold 是 ydotool 的守护进程，不跑的话点击/输入用不了
setup_ydotoold() {
  need_cmd ydotool || return 0
  pgrep -x ydotoold >/dev/null 2>&1 && return 0
  info "启动 ydotoold 守护进程"
  setsid ydotoold >/dev/null 2>&1 < /dev/null &
  sleep 1
  if pgrep -x ydotoold >/dev/null 2>&1; then
    dim "已启动（重启后需要再启动一次，或用 systemd 用户单元托管）"
  else
    warn "ydotoold 启动失败，模拟点击/输入可能不可用"
  fi
}

install_deps() {
  info "检查可选系统依赖（模拟点击/输入、截图、语音播报）"
  sudo_warmup || return 0
  install_pacman_deps
  install_aur_deps
  setup_ydotoold
  system_notes
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
  write_version_file
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

# 用 --purge 卸载前先停服务，否则进程会边跑边重建目录；从外部脚本升级时同样先停
stop_service() {
  need_cmd systemctl || return 0
  systemctl --user stop "$UNIT" >/dev/null 2>&1 || true
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

# 在安装目录留一个卸载入口，用户不必记住 install.sh 的路径和参数
install_uninstaller() {
  cat > "$INSTALL_DIR/uninstall.sh" <<'EOS'
#!/usr/bin/env bash
# DeckMeido 卸载入口（由 install.sh 自动生成）
#   bash uninstall.sh            停服务、删快捷方式，保留配置与工作区
#   bash uninstall.sh --purge    连配置、会话、工作区一起删
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -x "$HERE/install.sh" ]; then
  exec bash "$HERE/install.sh" uninstall "${1:-}"
fi
# 源码里的 install.sh 被删掉时的兜底：自己动手，逻辑与 install.sh 一致
APP=deckmeido
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
DESKTOP_DIR="$HOME/.local/share/applications"
DESKTOP_FOLDER="$(command -v xdg-user-dir >/dev/null 2>&1 && xdg-user-dir DESKTOP 2>/dev/null || true)"
[ -n "$DESKTOP_FOLDER" ] && [ "$DESKTOP_FOLDER" != "$HOME" ] || DESKTOP_FOLDER="$HOME/Desktop"
[ "$(id -u)" = "0" ] && { echo "不要用 sudo 运行"; exit 1; }
if command -v systemctl >/dev/null 2>&1; then
  systemctl --user disable --now deckmeido.service >/dev/null 2>&1 || true
  rm -f "$UNIT_DIR/deckmeido.service"
  systemctl --user daemon-reload || true
fi
rm -f "$DESKTOP_DIR/DeckMeido.desktop" "$DESKTOP_FOLDER/DeckMeido.desktop"
if [ "${1:-}" = "--purge" ]; then
  rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/$APP" "${DECKMEIDO_WORKSPACE:-$HOME/${APP}_workspace}"
fi
rm -rf "$HERE/.venv" "$HERE/server/sessions" "$HERE/server/static/shots" "$HERE/server/uploads"
echo "卸载完成。代码仍在 $HERE，完全删除：rm -rf \"$HERE\""
EOS
  chmod +x "$INSTALL_DIR/uninstall.sh"
}

# 版本号写进安装目录，网页设置页与升级检查都读它
write_version_file() {
  local ver
  ver=$(git -C "$INSTALL_DIR" describe --tags --always 2>/dev/null || true)
  [ -n "$ver" ] || ver=$(git -C "$HERE" describe --tags --always 2>/dev/null || true)
  [ -n "$ver" ] || ver="0.1.0"
  printf '%s\n' "${ver#v}" > "$INSTALL_DIR/VERSION"
  dim "版本：$(cat "$INSTALL_DIR/VERSION")"
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
  dim "卸载脚本:    bash $INSTALL_DIR/uninstall.sh   （加 --purge 连配置一起删）"
  echo
  dim "下一步：打开上面的地址，首次会弹出设置页，填 API 地址与 Key 即可对话"
  dim "以后升级：网页设置页点「检查更新」，或执行 bash $INSTALL_DIR/install.sh upgrade"
}

# ---------- 子命令 ----------
do_install() {
  local deps="${1:-}"
  check_prereq
  info "安装目录：$INSTALL_DIR"
  sync_files
  venv_install
  prepare_config
  stop_service
  install_service
  install_desktop
  install_uninstaller
  if [ "$deps" = "--deps" ]; then
    deps_intro
    install_deps
  else
    deps_intro
    if need_cmd sudo && ask_yes "这些控件属于「代替你动手操作」的核心能力，推荐安装。现在就用 sudo 安装吗？"; then
      install_deps
    else
      dim "已跳过。之后想装：bash install.sh install --deps"
    fi
  fi
  if need_cmd curl && wait_ready; then
    print_done
  else
    warn "服务还没就绪，查看日志：journalctl --user -u $UNIT -n 50"
  fi
}

do_upgrade() {
  local deps="${1:-}"
  [ -d "$INSTALL_DIR" ] || die "$INSTALL_DIR 不存在，请先执行安装"
  check_prereq
  # 先停服务：网页里点「立即升级」时，本进程就是那个服务，重启前先停掉；
  # 由 systemd 托管时停完会由后续 restart 重新拉起
  stop_service
  sync_files
  venv_install
  install_service
  install_desktop
  install_uninstaller
  if [ "$deps" = "--deps" ]; then
    install_deps
  else
    system_notes
  fi
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
  stop_service
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
  install|"")   do_install "${2:-}" ;;
  upgrade|update) do_upgrade "${2:-}" ;;
  uninstall)    do_uninstall "${2:-}" ;;
  -h|--help|help)
    grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -n 16 ;;
  *) die "未知命令：$1（可用：install / upgrade / uninstall）" ;;
esac
