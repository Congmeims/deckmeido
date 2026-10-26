#!/usr/bin/env bash
# DeckMeido 一键安装 / 升级 / 卸载
#
#   bash install.sh              安装或升级到当前目录（可重复执行）
#   bash install.sh install       同上
#   bash install.sh install --deps      顺带用 sudo 安装控制类插件（不再询问）
#   bash install.sh upgrade        升级代码与依赖，保留配置
#   bash install.sh upgrade --deps      升级后顺带检查并安装控制类插件
#   bash install.sh check          自检：逐个实测控制类控件是否真的能用
#   bash install.sh uninstall     卸载（默认保留 ~/.config/deckmeido 配置与工作区）
#   bash install.sh uninstall --purge   连配置一起删
#   bash install.sh service       生成 systemd 用户单元（默认不装，后端由客户端拉起）
#
# 控制类插件（ydotool / kdotool / wtype / espeak-ng / fakeroot）是系统包，
# 装它们需要 root：默认只在结尾提示并询问一次（y/N），回答 y 才走 sudo。
#
# 为什么装完还要自检：SteamOS 换镜像（BUILD_ID 变动）时，/usr 下由 pacman
# 装的包会被整片冲掉，而 pacman 的数据库记录不一定同步，所以只查
# `pacman -Q` 会出现「记录在、命令没了」的假象。install.sh check 一律实测。
# 同理 ydotoold 必须由 systemd 用户单元托管（enabled），裸跑的进程重启即失效。
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

# SteamOS 镜像号：换镜像时 /usr 下 pacman 装的包会被整片冲掉，这是本脚本
# 一切「装完还要实测」逻辑的根源。之前 BUILD_ID 从未赋值，导致镜像变动
# 检测（system_notes 里的提示）永远是死的，这里统一从 os-release 读。
BUILD_ID="$(sed -n 's/^BUILD_ID=//p' /etc/os-release 2>/dev/null | tr -d '"' | head -n1)"

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

# 装前预检：提前把「装到一半才炸」的情况暴露出来，而不是等 venv 建好、
# 依赖下完之后才因为磁盘满/端口被占而失败
preflight() {
  local problems=0
  # 1. 磁盘空间：PySide6 解包后约 700MB，venv 最坏按 1.5G 留余量
  local need_mb=1500 avail_mb=""
  avail_mb=$(df -Pm "$HOME" 2>/dev/null | awk 'NR==2 {print $4}')
  if [ -n "$avail_mb" ] && [ "$avail_mb" -lt "$need_mb" ]; then
    warn "家目录剩余 ${avail_mb}MB，安装建议至少 ${need_mb}MB（PySide6 体积较大）"
    problems=1
  else
    dim "磁盘空间：剩余 ${avail_mb:-未知}MB"
  fi

  # 2. 端口占用：只看是不是别人占着。已经被 DeckMeido 自己占着是正常情况
  #    （升级时后端就在跑），不能因此拦住安装
  if need_cmd ss; then
    local holder
    holder=$(ss -ltnp 2>/dev/null | awk -v p=":$PORT" '$4 ~ p {print $6; exit}')
    if [ -n "$holder" ]; then
      if printf '%s' "$holder" | grep -qi "$APP\|python"; then
        dim "端口 $PORT 由 DeckMeido 自己在用（升级时正常）"
      else
        warn "端口 $PORT 被别的进程占用：$holder"
        dim "先关掉它，或换端口：DECKMEIDO_PORT=18766 bash install.sh"
        problems=1
      fi
    else
      dim "端口 $PORT 空闲"
    fi
  fi

  # 3. 图形会话：客户端要靠它把窗口开出来，缺了就只能从桌面手动启动
  if [ -z "${XDG_RUNTIME_DIR:-}" ] || [ ! -d "${XDG_RUNTIME_DIR:-}" ]; then
    warn "XDG_RUNTIME_DIR 不存在，装完后请从桌面图标启动客户端"
    problems=1
  fi
  [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] || \
    warn "当前环境没有 DISPLAY / WAYLAND_DISPLAY，装完不会自动打开界面"

  [ "$problems" = 1 ] || dim "预检通过"
}

system_notes() {
  local miss=()
  need_cmd espeak-ng || miss+=("espeak-ng（语音播报回复）")
  need_cmd kdotool   || miss+=("kdotool（按窗口名切换/聚焦窗口）")
  need_cmd ydotool   || miss+=("ydotool（模拟鼠标点击、键盘输入）")
  need_cmd wtype     || miss+=("wtype（Wayland 原生打字，ydotool 的备选）")
  need_cmd spectacle || miss+=("spectacle（KWin 截图，一般自带）")
  if [ ${#miss[@]} -gt 0 ]; then
    warn "以下系统控件没装，相关功能会受限："
    for m in "${miss[@]}"; do dim "$m"; done
    dim "想自动安装：bash install.sh install --deps"
    # SteamOS 换镜像会冲掉 /usr 下 pacman 装的包，这情况最常见
    if [ -n "${BUILD_ID:-}" ] && [ -f "$HOME/.config/deckmeido/.deps-build-id" ]; then
      local prev; prev="$(cat "$HOME/.config/deckmeido/.deps-build-id" 2>/dev/null || true)"
      [ "$prev" = "$BUILD_ID" ] || dim "检测到 SteamOS 镜像已更新（$prev → ${BUILD_ID}），系统包可能被冲掉，重装即可恢复"
    fi
  fi
  # ydotool 装了但 ydotoold 没跑，点击/输入会直接报 socket 错误，这里提前点出来
  if need_cmd ydotool && ! pgrep -x ydotoold >/dev/null 2>&1; then
    warn "ydotool 已装但 ydotoold 守护进程没在运行，模拟点击/输入会失败"
    dim "修一次：bash install.sh check --fix   （会配置成开机自启）"
  fi
}

# ---------- 可选系统依赖 ----------
# 这些是「替你在桌面上动手」的核心控件，装它们必须 root。
# 默认在安装结尾说明用途并询问一次，或加 --deps 直接装。
# fakeroot 不是运行时控件，但用 paru/yay 编译 AUR 包（kdotool）必须要它，
# 缺了会在 makepkg 阶段报「fakeroot not found」，所以一起装。
DEPS_PACMAN=(espeak-ng ydotool wtype fakeroot)
# AUR 里叫 kdotool，编译出来很慢；优先用预编译的 kdotool-bin，
# 找不到 AUR helper 才提示手动装。
DEPS_AUR=(kdotool-bin)
DEPS_AUR_FALLBACK=(kdotool)

deps_intro() {
  # 安装前把每个控件是干什么的说清楚，用户才知道为什么要给 sudo
  local miss_pacman=() miss_aur=() miss_other=()
  for p in "${DEPS_PACMAN[@]}"; do need_cmd "$p" || miss_pacman+=("$p"); done
  need_cmd kdotool   || miss_aur+=("${DEPS_AUR[0]}")
  need_cmd spectacle || miss_other+=("spectacle")

  echo
  info "关于控制类系统控件（推荐安装）"
  dim "DeckMeido 的「帮你在桌面上操作」能力依赖下面这些系统包；"
  dim "不装也能聊天、截图、执行命令，但没法替你点鼠标、敲键盘、念出回复。"
  echo
  printf '  %sydotool%s   模拟鼠标点击 / 键盘输入（搭配 ydotoold 守护进程）\n' "$C_OK" "$C_END"
  printf '  %skdotool%s   按窗口标题查找并聚焦窗口，配合 ydotool 点击正确的位置\n' "$C_OK" "$C_END"
  printf '  %swtype%s     Wayland 原生打字，ydotool type 不支持中文时的备选\n' "$C_OK" "$C_END"
  printf '  %sespeak-ng%s 把回复念出来（语音播报）\n' "$C_OK" "$C_END"
  printf '  %sfakeroot%s  编译 AUR 包（kdotool）时 makepkg 必需的构建依赖\n' "$C_OK" "$C_END"
  printf '  %sspectacle%s KDE 截图工具，用于「看看当前屏幕」\n' "$C_OK" "$C_END"
  echo
  [ ${#miss_pacman[@]} -gt 0 ] && dim "待安装（pacman）：${miss_pacman[*]}"
  [ ${#miss_aur[@]}    -gt 0 ] && dim "待安装（AUR）：${miss_aur[*]}（需要 paru 或 yay）"
  [ ${#miss_other[@]}  -gt 0 ] && dim "未找到：${miss_other[*]}（KDE 一般自带，可忽略）"
  dim "安装过程会用 sudo 关闭 SteamOS 根分区只读保护并安装系统包，不会改动你的游戏与个人文件。"
}

ask_yes() {
  # 只有在 stdin 不是终端（例如被别处管道/程序调用）时才默认不装；
  # 直接粘贴多行命令时 stdin 仍是终端，正常询问
  if [ ! -t 0 ]; then
    if [ -r /dev/tty ]; then
      printf '%s?%s %s [y/N] ' "$C_WARN" "$C_END" "$1" > /dev/tty
      read -r ans < /dev/tty || return 1
    else
      return 1
    fi
  else
    printf '%s?%s %s [y/N] ' "$C_WARN" "$C_END" "$1"
    read -r ans || return 1
  fi
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
  local todo=() reinstall=() installed=()
  for p in "${DEPS_PACMAN[@]}"; do
    if pacman -Qq "$p" >/dev/null 2>&1; then
      # 记录在：再确认命令真的能用。SteamOS 换镜像后 /usr 被重建，
      # 包记录还留着但文件没了，这种情况必须「重装」而不是跳过
      if need_cmd "$p"; then installed+=("$p"); else reinstall+=("$p"); fi
    else
      todo+=("$p")
    fi
  done
  [ ${#todo[@]} -gt 0 ] || [ ${#reinstall[@]} -gt 0 ] || {
    dim "pacman 包已齐全：${installed[*]}"; return 0; }
  if [ ${#reinstall[@]} -gt 0 ]; then
    warn "以下包记录在但命令找不到（镜像更新冲掉了 /usr），重装：${reinstall[*]}"
  fi
  [ ${#todo[@]} -gt 0 ] && info "用 pacman 安装：${todo[*]}"
  # SteamOS 的根分区是只读的，装包前要先关掉只读保护。
  # 以前这里是 `|| true` 静默吞掉，结果 pacman 报一个跟只读毫无关系的错，
  # 用户完全看不出真正原因。现在失败就直接说清楚。
  if [ -f /etc/steamos-readonly.conf ] || need_cmd steamos-readonly; then
    if ! sudo steamos-readonly disable 2>/dev/null; then
      warn "关闭 SteamOS 只读保护失败，系统包无法安装"
      dim "手动执行：sudo steamos-readonly disable   然后再重跑本次命令"
      return 1
    fi
  fi
  # 先装缺的（--needed）；再对「记录在、文件没了」的强制重装（不能带 --needed，
  # 否则 pacman 会认为已装而直接跳过）。-Sy 而不 -Syu，避免顺带滚系统
  local log ok=1 pkgs=()
  [ ${#todo[@]} -gt 0 ] && pkgs+=("${todo[@]}")
  if [ ${#pkgs[@]} -gt 0 ]; then
    if log=$(sudo pacman -Sy --needed --noconfirm "${pkgs[@]}" 2>&1); then
      info "pacman 安装完成"
    else
      # 最常见失败原因：SteamOS 换镜像后 keyring 对不上，签名校验直接报错
      warn "pacman 安装失败，尝试修复 keyring 后重试"
      dim "$(printf '%s' "$log" | tail -5)"
      sudo pacman-key --init >/dev/null 2>&1 || true
      sudo pacman-key --populate archlinux holo >/dev/null 2>&1 || true
      if log=$(sudo pacman -Sy --needed --noconfirm "${pkgs[@]}" 2>&1); then
        info "修复 keyring 后安装成功"
      else
        warn "pacman 安装仍然失败："
        dim "$(printf '%s' "$log" | tail -20)"
        dim "手动排查：sudo pacman -Sy ${pkgs[*]}"
        ok=0
      fi
    fi
  fi
  if [ ${#reinstall[@]} -gt 0 ] && [ "$ok" = 1 ]; then
    if sudo pacman -S --noconfirm "${reinstall[@]}" >/dev/null 2>&1; then
      info "重装完成：${reinstall[*]}"
    else
      warn "重装失败：${reinstall[*]}"
      dim "手动排查：sudo pacman -S ${reinstall[*]}"
    fi
  fi
  # 装完立刻实测，避免「pacman 说装了但命令不在」的假成功
  local bad=()
  for p in "${todo[@]}" "${reinstall[@]}"; do need_cmd "$p" || bad+=("$p"); done
  if [ ${#bad[@]} -gt 0 ]; then
    warn "以下包装完仍找不到命令，可能是镜像更新冲掉了 /usr：${bad[*]}"
    dim "重跑一次：bash install.sh install --deps"
  fi
  record_deps_build
}

install_aur_deps() {
  local todo=() helper
  need_cmd kdotool || todo=("${DEPS_AUR[@]}")
  [ ${#todo[@]} -gt 0 ] || return 0
  helper=""
  for h in paru yay; do
    need_cmd "$h" && { helper="$h"; break; }
  done
  if [ -z "$helper" ]; then
    warn "kdotool 在 AUR 里，但没找到 paru / yay，跳过"
    dim "手动装法（推荐预编译版，不用编译）："
    dim "  git clone https://aur.archlinux.org/kdotool-bin.git && cd kdotool-bin && makepkg -si"
    dim "或先装个 AUR helper：sudo pacman -S --needed base-devel git && git clone https://aur.archlinux.org/paru-bin.git"
    return 0
  fi
  # 编译 AUR 包需要 fakeroot，缺了会在 makepkg 阶段直接失败
  if ! need_cmd fakeroot; then
    warn "缺少 fakeroot，AUR 编译会失败，先补装"
    sudo pacman -S --needed --noconfirm fakeroot >/dev/null 2>&1 || true
  fi
  info "用 $helper 安装：${todo[*]}"
  local log
  if log=$("$helper" -S --needed --noconfirm "${todo[@]}" 2>&1); then
    info "$helper 安装完成"
  else
    warn "$helper 安装失败，改用源码版（需要编译，耗时较久）：${DEPS_AUR_FALLBACK[*]}"
    dim "$(printf '%s' "$log" | tail -8)"
    if log=$("$helper" -S --needed --noconfirm "${DEPS_AUR_FALLBACK[@]}" 2>&1); then
      info "$helper 安装源码版完成"
    else
      warn "$helper 安装失败："
      dim "$(printf '%s' "$log" | tail -15)"
      dim "手动装法：git clone https://aur.archlinux.org/kdotool-bin.git && cd kdotool-bin && makepkg -si"
    fi
  fi
}

# ydotoold 是 ydotool 的守护进程，不跑的话点击/输入用不了。
# 注意：不能用 `ydotoold &` 裸跑——那种进程重启就没了，用户会以为装了却不能用。
# 包自带 /usr/lib/systemd/user/ydotool.service（Restart=always），交给 systemd 托管。
setup_ydotoold() {
  need_cmd ydotool || return 0
  if ! need_cmd systemctl; then
    # 没有 systemd 的极端情况才退回裸跑
    pgrep -x ydotoold >/dev/null 2>&1 && return 0
    setsid ydotoold >/dev/null 2>&1 < /dev/null &
    sleep 1
    pgrep -x ydotoold >/dev/null 2>&1 \
      && dim "ydotoold 已启动（无 systemd，重启后需手动再启动）" \
      || warn "ydotoold 启动失败，模拟点击/输入可能不可用"
    return 0
  fi
  # 已有裸跑进程时先停掉，否则 systemd 抢占 socket 会失败
  if pgrep -x ydotoold >/dev/null 2>&1 && \
     ! systemctl --user is-active ydotool.service >/dev/null 2>&1; then
    dim "停掉手动启动的 ydotoold，改由 systemd 托管"
    pkill -x ydotoold 2>/dev/null || true
    sleep 1
    rm -f "/run/user/$(id -u)/.ydotool_socket"
  fi
  # 注意：is-enabled 对 static/indirect 等状态也返回 0，必须比对输出字符串，
  # 否则会把「没设自启」误判成已设好
  local en ac
  en=$(systemctl --user is-enabled ydotool.service 2>/dev/null || true)
  ac=$(systemctl --user is-active  ydotool.service 2>/dev/null || true)
  if [ "$en" = enabled ] && [ "$ac" = active ]; then
    dim "ydotoold 已由 systemd 托管并开机自启"
    return 0
  fi
  if [ "$en" != enabled ]; then
    info "设置 ydotool 开机自启（之前：${en:-未知}）"
    systemctl --user enable ydotool.service >/dev/null 2>&1 || \
      warn "enable ydotool.service 失败，重启后可能要手动启动"
  fi
  # 已 enabled 但没跑（手动 stop 过 / 刚开机还没拉起）时也要拉起来
  if [ "$ac" != active ]; then
    info "启动 ydotool 守护进程（之前：${ac:-未知}）"
    systemctl --user start ydotool.service >/dev/null 2>&1 || true
    sleep 1
  fi
  if systemctl --user is-active ydotool.service >/dev/null 2>&1; then
    dim "ydotoold 已由 systemd 托管，重启后自动恢复"
  else
    warn "ydotool.service 没起来，模拟点击/输入可能不可用"
    dim "手动排查：systemctl --user status ydotool.service"
  fi
}

# 记下这次装依赖时的 SteamOS 镜像号，下次镜像变动就能提醒用户重装
record_deps_build() {
  mkdir -p "$CONFIG_DIR"
  printf '%s\n' "${BUILD_ID:-unknown}" > "$CONFIG_DIR/.deps-build-id" 2>/dev/null || true
}

# ---------- 控件自检 ----------
# 只查 pacman -Q 不够：SteamOS 换镜像会冲掉 /usr 下的包而数据库仍留记录。
# 所以这里逐项实测「命令在不在 + 真的能不能跑」。
probe_ydotool() {
  need_cmd ydotool || { echo "未安装（模拟鼠标点击、键盘输入）"; return 1; }
  need_cmd ydotoold || { echo "守护进程缺失，需重装 ydotool"; return 1; }
  local sock="/run/user/$(id -u)/.ydotool_socket"
  [ -S "$sock" ] || { echo "ydotoold 守护进程没在跑（$sock 不存在），点击/输入会失败"; return 1; }
  # 只连 socket 验活，不发 mousemove 之类的命令——那会把用户的光标真的挪走。
  # ydotoold 用 SOCK_DGRAM，用 SOCK_STREAM 连会报 Protocol wrong type for socket
  if python3 - "$sock" <<'PY' >/dev/null 2>&1
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
s.settimeout(1.0)
try:
    s.connect(sys.argv[1])
finally:
    s.close()
PY
  then
    return 0
  fi
  echo "socket 通信失败（ydotoold 可能在重启中）"; return 1
}
# ydotoold 的托管状态：enabled 决定重启后还在不在，active 决定现在能不能用
ydotoold_state() {
  if ! need_cmd systemctl; then
    pgrep -x ydotoold >/dev/null 2>&1 && echo "运行中（无 systemd，重启需手动）" || echo "未运行"
    return 0
  fi
  local en ac
  en=$(systemctl --user is-enabled ydotool.service 2>/dev/null || echo unknown)
  ac=$(systemctl --user is-active  ydotool.service 2>/dev/null || echo unknown)
  if [ "$ac" = active ] && [ "$en" = enabled ]; then
    echo "运行中 · 已设开机自启"
  elif [ "$ac" = active ]; then
    echo "运行中 · 未设开机自启（重启会失效）"
  elif [ "$en" = enabled ]; then
    echo "已设开机自启 · 当前未运行（手动 stop 过，或刚开机还没拉起）"
  else
    echo "未运行 · 未设开机自启"
  fi
}
probe_kdotool() {
  need_cmd kdotool || { echo "未安装（按窗口名聚焦）"; return 1; }
  KDE_SESSION_VERSION=6 kdotool getactivewindow >/dev/null 2>&1 && return 0
  echo "调用失败（需要 KDE Plasma 6 会话）"; return 1
}
probe_wtype() {
  need_cmd wtype || { echo "未安装（Wayland 原生打字）"; return 1; }
  wtype >/dev/null 2>&1
  # 无参数时 wtype 返回 1 并打印用法；返回 127/命令不存在才是真坏
  [ $? -ne 127 ] && return 0
  echo "命令无法执行"; return 1
}
probe_espeak() {
  need_cmd espeak-ng || { echo "未安装（语音播报）"; return 1; }
  espeak-ng --version >/dev/null 2>&1 && return 0
  echo "调用失败"; return 1
}
probe_fakeroot() {
  need_cmd fakeroot || { echo "未安装（编译 AUR 包需要）"; return 1; }
  fakeroot true >/dev/null 2>&1 && return 0
  echo "调用失败"; return 1
}
probe_spectacle() {
  need_cmd spectacle || { echo "未安装（KWin 截图，KDE 一般自带）"; return 1; }
  # 只查得到二进制不够，还要确认它能跑起来。这里必须设 offscreen：
  # 在无 DISPLAY 的环境（systemd 单元、ssh、cron）下 Qt6 会直接 abort 并写 core dump。
  # 用 --version 而不是 --help，避开 GUI 初始化路径。
  QT_QPA_PLATFORM=offscreen DISPLAY= WAYLAND_DISPLAY= \
    spectacle --version >/dev/null 2>&1 || { echo "调用异常（可能在无图形环境下崩溃）"; return 1; }
  return 0
}

# 返回缺失/异常的控件名列表（空格分隔）
deps_broken() {
  local broken=() f
  for f in probe_ydotool probe_kdotool probe_wtype probe_espeak probe_fakeroot probe_spectacle; do
    "$f" >/dev/null 2>&1 || broken+=("${f#probe_}")
  done
  echo "${broken[*]:-}"
}

do_check() {
  local fix=0
  [ "${1:-}" = "--fix" ] && fix=1
  check_user
  info "控制类控件自检"
  local broken=() name
  for name in ydotool kdotool wtype espeak fakeroot spectacle; do
    local msg
    if msg=$("probe_$name" 2>&1); then
      printf '  %s● OK %s %-10s %s\n' "$C_OK" "$C_END" "$name" "$(probe_label "$name")"
    else
      printf '  %s○ BAD%s %-10s %s\n' "$C_ERR" "$C_END" "$name" "$msg"
      broken+=("$name")
    fi
  done
  echo
  # ydotoold 是否被 systemd 托管，单独一行看清楚：这是「重启后还能不能用」的关键
  need_cmd ydotool && { printf '  %-10s %s\n' "ydotoold" "$(ydotoold_state)"; echo; }
  if [ ${#broken[@]} -eq 0 ]; then
    info "全部正常"
    return 0
  fi
  warn "异常项：${broken[*]}"
  if [ "$fix" = 1 ]; then
    info "尝试自动修复"
    sudo_warmup || return 0
    install_pacman_deps
    install_aur_deps
    setup_ydotoold
    echo
    do_check
  else
    dim "自动修复：bash install.sh check --fix"
  fi
}

probe_label() {
  case "$1" in
    ydotool)  echo "模拟鼠标点击 / 键盘输入" ;;
    kdotool)  echo "按窗口标题聚焦窗口" ;;
    wtype)    echo "Wayland 原生打字（备选）" ;;
    espeak)   echo "语音播报回复" ;;
    fakeroot) echo "编译 AUR 包的构建依赖" ;;
    spectacle) echo "KWin 截图" ;;
  esac
}

# 镜像更新后自动补齐：SteamOS 换镜像会冲掉 /usr 下 pacman 装的包
# （wtype 就这么消失过一次）。已有 sudo 授权时不必再问用户，直接重装。
maybe_autorepair_after_image_update() {
  [ -n "$BUILD_ID" ] || return 0
  local stamp="$CONFIG_DIR/.deps-build-id"
  [ -f "$stamp" ] || return 0
  local prev; prev="$(cat "$stamp" 2>/dev/null || true)"
  [ "$prev" = "$BUILD_ID" ] && return 0
  info "检测到 SteamOS 镜像已更新（${prev:-未知} → $BUILD_ID）"
  dim "镜像更新会重建 /usr，之前装的控件可能已被冲掉，现在重新检查一遍"
  local broken; broken="$(deps_broken)"
  if [ -z "$broken" ]; then
    dim "实测控件都还在，无需重装"
    record_deps_build
    return 0
  fi
  warn "以下控件已被冲掉：$broken"
  sudo_warmup || return 0
  install_pacman_deps
  install_aur_deps
  setup_ydotoold
  record_deps_build
  local left; left="$(deps_broken)"
  if [ -n "$left" ]; then
    warn "重装后仍不可用：$left"
    dim "逐个看原因：bash install.sh check"
  else
    info "控件已全部恢复"
  fi
}

install_deps() {
  info "检查可选系统依赖（模拟点击/输入、截图、语音播报）"
  sudo_warmup || return 0
  install_pacman_deps
  install_aur_deps
  setup_ydotoold
  # 装完跑一遍实测，把「记录在、命令没了」的情况直接暴露出来
  local broken; broken="$(deps_broken)"
  if [ -n "$broken" ]; then
    warn "以下控件装完仍不可用：$broken"
    dim "逐个看原因：bash install.sh check"
  fi
  system_notes
}

# ---------- 代码同步 ----------
# 清单式同步：rsync 只管「源里有 → 目标也要有」，源里已经删掉的文件会
# 永远留在安装目录里。升级几次之后目录里堆满早已废弃的模块，最坑的是
# 那些文件名还在、但早就没人 import 的旧文件，排查时很容易看错。
#
# 这里反向再走一遍：安装目录里存在、源码目录里已不存在的代码文件，删掉。
# 只处理下面这些已知代码目录，用户数据（config.toml / sessions / uploads /
# shots / *.log / VERSION）一概不碰。
CODE_DIRS=(server client assets deploy tools)
USER_DATA_RE='(^|/)(config\.toml|sessions|uploads|shots)(/|$)|\.log$|(^|/)VERSION$|(^|/)__pycache__(/|$)'

prune_stale() {
  [ -d "$INSTALL_DIR" ] || return 0
  local removed=0 rel d
  for d in "${CODE_DIRS[@]}"; do
    [ -d "$INSTALL_DIR/$d" ] || continue
    # 只在安装目录里扫，逐个比对源码里是否有同名文件
    while IFS= read -r rel; do
      [ -n "$rel" ] || continue
      printf '%s' "$rel" | grep -Eq "$USER_DATA_RE" && continue
      [ -e "$HERE/$rel" ] && continue
      rm -f "$INSTALL_DIR/$rel" && removed=$((removed + 1))
    done < <(cd "$INSTALL_DIR" && find "$d" -type f 2>/dev/null)
  done
  # 删完之后可能留下空目录，一并收掉（只收代码目录内的）
  for d in "${CODE_DIRS[@]}"; do
    [ -d "$INSTALL_DIR/$d" ] || continue
    find "$INSTALL_DIR/$d" -mindepth 1 -type d -empty -delete 2>/dev/null || true
  done
  [ "$removed" -gt 0 ] && dim "清理了 $removed 个新版本中已不存在的旧文件"
  return 0
}

sync_files() {
  [ -f "$HERE/install.sh" ] || die "当前目录不像 DeckMeido 源码目录（缺 install.sh）"
  if [ "$HERE" = "$(cd "$INSTALL_DIR" 2>/dev/null && pwd -P)" ]; then
    info "已在使用目录 $INSTALL_DIR"
    write_version_file
    return
  fi
  if [ -d "$INSTALL_DIR" ]; then
    info "同步代码到 $INSTALL_DIR"
    prune_stale
  else
    info "安装到 $INSTALL_DIR"
  fi
  mkdir -p "$INSTALL_DIR"
  local items=(server client assets deploy tools requirements.txt README.md install.sh)
  # RELEASE-NOTES.md 只在发布包里存在，有就一起带上，方便用户在安装目录里查更新内容
  [ -f "$HERE/RELEASE-NOTES.md" ] && items+=(RELEASE-NOTES.md)
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
  # 不再安装 systemd 开机自启服务。
  #
  # 历史原因：以前 install.sh 装一个 deckmeido.service，客户端启动时又自己拉一份后端，
  # 两份抢同一个端口——谁先绑定谁对外服务，而它们各有一份内存历史，
  # 重启时被另一半抢到，用户看到的就是「聊天记录丢了」。
  # 现在后端只由客户端（桌面快捷方式）拉起，退出时一并收走。
  # 这里仅做清理：把老版本装过的单元停掉删掉，避免升级后还有残留一起抢端口。
  need_cmd systemctl || return 0
  systemctl --user disable --now "$UNIT" >/dev/null 2>&1 || true
  rm -f "$UNIT_DIR/$UNIT"
  systemctl --user daemon-reload || true
  # 兜底：万一还有裸跑的后端占着端口，停掉它，把入口让回客户端
  if need_cmd fuser; then
    fuser -k -TERM "$PORT/tcp" >/dev/null 2>&1 || true
    sleep 1
  fi
}

# 用 --purge 卸载前先停服务，否则进程会边跑边重建目录；从外部脚本升级时同样先停
stop_service() {
  need_cmd systemctl || return 0
  systemctl --user stop "$UNIT" >/dev/null 2>&1 || true
}

# 手动安装 systemd 单元（默认不装：后端由客户端拉起，见 install_service 注释）
render_service() {
  sed "s|__INSTALL_DIR__|$INSTALL_DIR|g; s|__PORT__|$PORT|g" \
    "$INSTALL_DIR/deploy/$UNIT" > "$UNIT_DIR/$UNIT"
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

# 在安装目录留一个卸载入口，用户不必记住 install.sh 的路径和参数。
# 逻辑只有 do_uninstall 一份，这里只做转发；install.sh 被删了才走兜底分支。
install_uninstaller() {
  cat > "$INSTALL_DIR/uninstall.sh" <<'EOS'
#!/usr/bin/env bash
# DeckMeido 卸载入口（由 install.sh 自动生成）
#   bash uninstall.sh            停服务、删快捷方式，保留配置与工作区
#   bash uninstall.sh --purge    连配置、会话、工作区一起删
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$HERE/install.sh" ]; then
  exec bash "$HERE/install.sh" uninstall "${1:-}"
fi
# ---- 兜底：install.sh 已经不在了，只能自己动手 ----
APP=deckmeido
PORT="${DECKMEIDO_PORT:-18765}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/$APP"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
DESKTOP_DIR="$HOME/.local/share/applications"
DESKTOP_FOLDER="$(command -v xdg-user-dir >/dev/null 2>&1 && xdg-user-dir DESKTOP 2>/dev/null || true)"
[ -n "$DESKTOP_FOLDER" ] && [ "$DESKTOP_FOLDER" != "$HOME" ] || DESKTOP_FOLDER="$HOME/Desktop"
[ "$(id -u)" = "0" ] && { echo "不要用 sudo 运行"; exit 1; }

# 先取消正在跑的任务，再关进程
if command -v curl >/dev/null 2>&1 && [ -f "$HERE/server/config.toml" ]; then
  TOK="$(grep -oP '^token\s*=\s*"\K[^"]*' "$HERE/server/config.toml" 2>/dev/null | head -n1 || true)"
  curl -fsS -m 2 -X POST -H "X-Token: $TOK" "http://127.0.0.1:$PORT/api/cancel" >/dev/null 2>&1 || true
fi
pkill -f "$HERE/client/main.py" 2>/dev/null || true
if command -v systemctl >/dev/null 2>&1; then
  systemctl --user disable --now deckmeido.service >/dev/null 2>&1 || true
  rm -f "$UNIT_DIR/deckmeido.service"
  systemctl --user daemon-reload || true
fi
pkill -f "$HERE/server/main.py" 2>/dev/null || true
pkill -f "$HERE/server/overlay.py" 2>/dev/null || true
rm -f "$DESKTOP_DIR/DeckMeido.desktop" "$DESKTOP_FOLDER/DeckMeido.desktop"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
if [ "${1:-}" = "--purge" ]; then
  WS="$(grep -oP '^\s*workspace\s*=\s*"\K[^"]*' "$HERE/server/config.toml" 2>/dev/null | head -n1 || true)"
  [ -n "$WS" ] || WS="${DECKMEIDO_WORKSPACE:-$HOME/${APP}_workspace}"
  case "$WS" in "~"/*) WS="$HOME/${WS#\~/}" ;; /*) ;; *) WS="$HOME/$WS" ;; esac
  rm -rf "$CONFIG_DIR"
  case "$WS" in ""|"/"|"$HOME") echo "工作区路径异常（$WS），跳过删除" ;; *) rm -rf "$WS" ;; esac
fi
rm -rf "$HERE/.venv" "$HERE/server/sessions" "$HERE/server/static/shots" "$HERE/server/uploads"
rm -f "$HERE/VERSION" "$HERE/update.log"
echo "卸载完成。代码仍在 $HERE，完全删除：rm -rf \"$HERE\""
EOS
  chmod +x "$INSTALL_DIR/uninstall.sh"
}

# 版本号写进安装目录，网页设置页与升级检查都读它
write_version_file() {
  # 发布包里带一份 VERSION（make_release.sh 按 tag 写入），tar 安装时没有
  # git 仓库，只能靠它；git 安装则用 describe，能反映「比 tag 多几个提交」
  local ver=""
  if [ -f "$HERE/VERSION" ]; then
    ver=$(head -n1 "$HERE/VERSION" 2>/dev/null || true)
  fi
  if [ -z "$ver" ]; then
    ver=$(git -C "$INSTALL_DIR" describe --tags --always 2>/dev/null || true)
  fi
  [ -n "$ver" ] || ver=$(git -C "$HERE" describe --tags --always 2>/dev/null || true)
  [ -n "$ver" ] || ver="0.0.0"
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
  for _ in $(seq 1 3); do
    if curl -fsS -o /dev/null "$url" 2>/dev/null; then return 0; fi
    sleep 0.3
  done
  return 1
}

launch_client() {
  # 服务由客户端拉起，所以装完直接帮用户开一次客户端，服务就齐了
  local exe="$INSTALL_DIR/.venv/bin/python"
  local cli="$INSTALL_DIR/client/main.py"
  [ -x "$exe" ] && [ -f "$cli" ] || return 1
  if [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
    setsid "$exe" "$cli" >/dev/null 2>&1 &
    return 0
  fi
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
  dim "访问令牌默认为 meido 可在客户端设置页修改"
  dim "配置文件:    $INSTALL_DIR/server/config.toml"
  dim "卸载脚本:    bash $INSTALL_DIR/uninstall.sh   （加 --purge 连配置一起删）"
  echo
  dim "下一步：打开上面的地址，首次会弹出设置页，填 API 地址与 Key 即可对话"
  dim "以后升级：网页设置页点「检查更新」，或执行 bash $INSTALL_DIR/install.sh upgrade"
  echo
  dim "当前版本:    $(cat "$INSTALL_DIR/VERSION" 2>/dev/null || echo '未知')"
}

# ---------- 子命令 ----------
do_install() {
  local deps="${1:-}"
  check_prereq
  info "安装目录：$INSTALL_DIR"
  preflight
  sync_files
  venv_install
  prepare_config
  stop_service
  install_service
  install_desktop
  install_uninstaller
  if [ "$deps" = "--deps" ]; then
    deps_intro
    maybe_autorepair_after_image_update
    install_deps
  else
    deps_intro
    if need_cmd sudo && ask_yes "这些控件属于「代替你动手操作」的核心能力，推荐安装。现在就用 sudo 安装吗？"; then
      install_deps
    else
      dim "已跳过。之后想装：bash install.sh install --deps"
    fi
  fi
  # 服务由客户端拉起：装完顺手开一次客户端，后台服务就一并起来了
  if launch_client && wait_ready; then
    print_done
  else
    warn "客户端未能自动启动，请双击桌面上的 DeckMeido 图标（服务会随之拉起）"
  fi
}

# 停掉客户端：客户端是后端的父进程，还会在 watchdog 里自动补拉后端。
# 升级过程中它要是还活着，会一边同步代码一边被重新拉起旧后端，
# 端口和内存里的历史都对不上。升级完成后由 launch_client 重新开。
stop_client() {
  pgrep -f "$INSTALL_DIR/client/main.py" >/dev/null 2>&1 || return 0
  dim "关闭客户端（升级后自动重开）"
  pkill -f "$INSTALL_DIR/client/main.py" 2>/dev/null || true
  local i
  for i in $(seq 1 20); do
    pgrep -f "$INSTALL_DIR/client/main.py" >/dev/null 2>&1 || return 0
    sleep 0.2
  done
  pkill -9 -f "$INSTALL_DIR/client/main.py" 2>/dev/null || true
}

do_upgrade() {
  local deps="${1:-}"
  [ -d "$INSTALL_DIR" ] || die "$INSTALL_DIR 不存在，请先执行安装"
  check_prereq
  # 顺序要紧：先停客户端（否则它的 watchdog 会把旧后端拉回来），再停服务
  stop_client
  stop_service
  sync_files
  venv_install
  install_service
  install_desktop
  install_uninstaller
  if [ "$deps" = "--deps" ]; then
    maybe_autorepair_after_image_update
    install_deps
  else
    system_notes
  fi
  # 服务由客户端拉起：升级后关掉旧客户端再开新的，新代码才会生效
  launch_client && wait_ready || true
  if need_cmd curl && wait_ready; then
    print_done
  else
    warn "升级完成。若界面还是旧版本，请关掉托盘里的 DeckMeido 再从桌面图标重开"
  fi
}

# 正在跑的任务要先取消：卸载时把后端直接杀掉，正在执行的命令会变成孤儿进程
cancel_running_turn() {
  local token
  token=$(read_token)
  need_cmd curl || return 0
  curl -fsS -m 2 -X POST -H "X-Token: $token" \
    "http://127.0.0.1:$PORT/api/cancel" >/dev/null 2>&1 || true
}

# 工作区真实路径：优先读配置里的 workspace，删不到才退回默认目录。
# 以前直接删 $HOME/deckmeido_workspace，而配置里指到别处的工作区根本删不到。
workspace_dir() {
  local cfg="$INSTALL_DIR/server/config.toml" ws=""
  if [ -f "$cfg" ]; then
    ws=$(grep -oP '^\s*workspace\s*=\s*"\K[^"]*' "$cfg" 2>/dev/null | head -n1)
  fi
  [ -n "$ws" ] || ws="${DECKMEIDO_WORKSPACE:-$HOME/${APP}_workspace}"
  # 配置里可能写的是相对路径或 ~，统一展开
  case "$ws" in
    "~"/*) ws="$HOME/${ws#\~/}" ;;
    /*) ;;
    *) ws="$HOME/$ws" ;;
  esac
  printf '%s\n' "$ws"
}

do_uninstall() {
  local purge=0
  [ "${1:-}" = "--purge" ] && purge=1
  check_user
  info "卸载 DeckMeido"
  # 先取消正在执行的任务，再停客户端和后端，避免留下跑了一半的命令
  cancel_running_turn
  # 客户端是后端的父进程，先让它退出，后端会跟着收走
  if pgrep -f "$INSTALL_DIR/client/main.py" >/dev/null 2>&1; then
    dim "关闭客户端"
    pkill -f "$INSTALL_DIR/client/main.py" 2>/dev/null || true
    sleep 1
    pkill -9 -f "$INSTALL_DIR/client/main.py" 2>/dev/null || true
  fi
  stop_service
  if need_cmd systemctl; then
    dim "移除用户服务"
    systemctl --user disable --now "$UNIT" >/dev/null 2>&1 || true
    rm -f "$UNIT_DIR/$UNIT"
    systemctl --user daemon-reload || true
  fi
  # 兜底：后端被独立拉起时（旧版本装过 systemd 单元）也要收掉，否则卸载后
  # 它还在 18765 上跑，下次安装会撞端口
  pkill -f "$INSTALL_DIR/server/main.py" 2>/dev/null || true
  pkill -f "$INSTALL_DIR/server/overlay.py" 2>/dev/null || true
  sleep 1

  rm -f "$DESKTOP_DIR/DeckMeido.desktop"
  rm -f "$DESKTOP_FOLDER/DeckMeido.desktop"
  need_cmd update-desktop-database && update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
  # 桌面文件被删后，KDE 的图标缓存里还会留着入口，通知它刷新一次
  need_cmd kbuildsycoca6 && kbuildsycoca6 >/dev/null 2>&1 || true
  need_cmd kbuildsycoca5 && kbuildsycoca5 >/dev/null 2>&1 || true

  if [ "$purge" = 1 ]; then
    local ws; ws="$(workspace_dir)"
    warn "删除配置、会话与工作区（不可恢复）"
    dim "配置：$CONFIG_DIR"
    dim "工作区：$ws"
    rm -rf "$CONFIG_DIR"
    case "$ws" in
      ""|"/"|"$HOME") warn "工作区路径异常（$ws），跳过删除" ;;
      *) rm -rf "$ws" ;;
    esac
  fi
  # 只删本仓库自带的运行产物，避免误删用户数据
  rm -rf "$INSTALL_DIR/server/sessions" "$INSTALL_DIR/server/static/shots" \
         "$INSTALL_DIR/server/uploads" "$INSTALL_DIR/.venv"
  rm -f "$INSTALL_DIR/VERSION" "$INSTALL_DIR/update.log" "$INSTALL_DIR/uninstall.sh"
  info "卸载完成"
  dim "代码仍保留在 $INSTALL_DIR；完全删除：rm -rf $INSTALL_DIR"
}

case "${1:-install}" in
  install|"")   do_install "${2:-}" ;;
  upgrade|update) do_upgrade "${2:-}" ;;
  check)        do_check "${2:-}" ;;
  uninstall)    do_uninstall "${2:-}" ;;
  service)      check_user; mkdir -p "$UNIT_DIR"; render_service
                info "已写入 $UNIT_DIR/$UNIT"
                dim "启用并启动：systemctl --user daemon-reload && systemctl --user enable --now $UNIT" ;;
  -h|--help|help)
    grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -n 22 ;;
  *) die "未知命令：$1（可用：install / upgrade / check / uninstall / service）" ;;
esac
