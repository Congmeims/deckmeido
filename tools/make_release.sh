#!/usr/bin/env bash
# 打一个可直接分发的 tar.gz（不含 .git / 虚拟环境 / 用户配置）
#
#   bash tools/make_release.sh           版本号取 git describe
#   bash tools/make_release.sh v1.0.0    指定版本号
# 产物：dist/deckmeido-<版本>.tar.gz
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VER="${1:-$(git -C "$ROOT" describe --tags --always 2>/dev/null || echo dev)}"
NAME="deckmeido-${VER#v}"
OUT="$ROOT/dist"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$OUT" "$STAGE/$NAME"
cd "$ROOT"
cp -r server client assets deploy tools requirements.txt README.md install.sh "$STAGE/$NAME/"
# 清掉不该进包的东西
rm -rf "$STAGE/$NAME/server/config.toml" \
       "$STAGE/$NAME/server/sessions" \
       "$STAGE/$NAME/server/uploads" \
       "$STAGE/$NAME/server/static/shots" \
       "$STAGE/$NAME/server/overlay.log"
find "$STAGE/$NAME" -name '__pycache__' -type d -prune -exec rm -rf {} +
chmod +x "$STAGE/$NAME/install.sh"

tar -C "$STAGE" -czf "$OUT/$NAME.tar.gz" "$NAME"
echo "已生成 $OUT/$NAME.tar.gz"
