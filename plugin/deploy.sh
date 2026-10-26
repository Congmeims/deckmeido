#!/usr/bin/env bash
# DeckMeido 的 Decky 插件部署脚本。
#
# 插件本体放在 ~/homebrew/plugins/DeckMeido，用软链接指回本仓库的 plugin/，
# 这样改完代码只要重启 Decky 就能生效，不用重复部署。
#
# 用法：
#   bash plugin/deploy.sh               构建并部署
#   bash plugin/deploy.sh --remove      卸载插件
#   bash plugin/deploy.sh --no-build    跳过前端构建（dist 已存在时用）
#   bash plugin/deploy.sh --check       只报状态，不改任何东西（install.sh 用）
#
# 退出码：0 = 已就绪 / 成功；1 = 失败；2 = 环境不具备（没装 Decky、没装 npm），
# 这种情况 install.sh 不该当错误处理，只提示一句。
#
# 被 install.sh 调用时（DECKMEIDO_NONINTERACTIVE=1）不打印颜色和多余提示。
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGINS_ROOT="${DECKY_PLUGINS_DIR:-$HOME/homebrew/plugins}"
TARGET="$PLUGINS_ROOT/DeckMeido"
NONINTERACTIVE="${DECKMEIDO_NONINTERACTIVE:-0}"

C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_DIM=$'\033[2m'; C_END=$'\033[0m'
if [ ! -t 1 ] || [ "$NONINTERACTIVE" = "1" ]; then C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_END=""; fi
info() { printf '%s==>%s %s\n' "$C_OK" "$C_END" "$*"; }
warn() { printf '%s[!]%s %s\n' "$C_WARN" "$C_END" "$*"; }
die()  { printf '%s[x]%s %s\n' "$C_ERR" "$C_END" "$*" >&2; exit 1; }
dim()  { printf '%s    %s%s\n' "$C_DIM" "$*" "$C_END"; }

case "${1:-}" in
  --remove) MODE=remove ;;
  --check)  MODE=check ;;
  --no-build) MODE=deploy; BUILD=0 ;;
  "")       MODE=deploy; BUILD=1 ;;
  *)        die "不认识的参数：$1（可用：--remove / --check / --no-build）" ;;
esac
BUILD="${BUILD:-1}"

# ---- 只看状态 ----
if [ "$MODE" = "check" ]; then
  [ -d "$PLUGINS_ROOT" ] || { dim "没装 Decky Loader（$PLUGINS_ROOT 不存在），跳过插件"; exit 2; }
  [ -L "$TARGET" ] && info "Decky 插件已链接到 $PLUGIN_DIR" || { warn "Decky 插件未部署"; exit 1; }
  [ -f "$PLUGIN_DIR/dist/index.js" ] || { warn "插件缺 dist/index.js，需要构建"; exit 1; }
  exit 0
fi

# ---- 卸载 ----
if [ "$MODE" = "remove" ]; then
  if [ -L "$TARGET" ] || [ -d "$TARGET" ]; then
    rm -rf "$TARGET"
    info "已移除 $TARGET"
    dim "重启 Decky（设置 → Decky Loader → 重新载入）后插件从 QAM 消失"
  else
    info "插件本来就没装"
  fi
  exit 0
fi

# ---- 前置检查 ----
if [ ! -d "$PLUGINS_ROOT" ]; then
  warn "没找到 $PLUGINS_ROOT，跳过插件部署"
  dim "装好 Decky Loader 后再跑一次：bash plugin/deploy.sh"
  exit 2
fi

if [ "$(id -u)" = "0" ]; then
  die "不要用 sudo/root 执行：插件要装在当前用户家目录下"
fi

# ---- 构建前端 ----
if [ "$BUILD" = "1" ]; then
  need_build=1
  if [ -d "$PLUGIN_DIR/dist" ] && [ -f "$PLUGIN_DIR/package.json" ]; then
    # dist 比源码新就跳过，省得每次都等 npm
    if [ -z "$(find "$PLUGIN_DIR/src" -newer "$PLUGIN_DIR/dist" -type f 2>/dev/null)" ]; then
      need_build=0
      info "dist 已是最新，跳过构建"
    fi
  fi

  if [ "$need_build" = "1" ]; then
    if ! command -v npm >/dev/null 2>&1; then
      warn "没装 npm，无法构建插件前端"
      dim "装好 nodejs/npm 后再跑：bash plugin/deploy.sh"
      exit 2
    fi
    info "构建前端"
    cd "$PLUGIN_DIR"
    if [ ! -d node_modules ]; then
      npm install --no-audit --no-fund
    fi
    npm run build
    [ -f "$PLUGIN_DIR/dist/index.js" ] || die "构建完成但没有 dist/index.js"
  fi
fi

[ -f "$PLUGIN_DIR/dist/index.js" ] || die "缺少 dist/index.js，先不带 --no-build 跑一次"

# ---- 部署（软链接，不复制）----
if [ -e "$TARGET" ] && [ ! -L "$TARGET" ]; then
  warn "$TARGET 不是软链接（可能是之前复制进去的旧版本），先挪走"
  mv "$TARGET" "$TARGET.bak.$(date +%s)"
fi
ln -sfn "$PLUGIN_DIR" "$TARGET"
info "已链接 $TARGET → $PLUGIN_DIR"

# ---- Python 依赖：插件直接用系统 python，不建 venv ----
# Decky 自带 python 环境，main.py 只用到标准库（tomllib/base64/json），
# 不需要额外装包。将来要加依赖必须写进 plugin/requirements.txt，
# 由用户手动 pip 装到 Decky 的 python 里。
if [ -f "$PLUGIN_DIR/requirements.txt" ]; then
  warn "plugin/requirements.txt 存在，但本脚本不会自动 pip 安装"
  dim "Decky 的 python 环境比较敏感，装包请手动确认后再执行"
fi

info "部署完成"
dim "到 设置 → Decky Loader 里点「重新载入」，或重启游戏模式"
dim "之后 QAM 里会出现 DeckMeido 面板"
