#!/usr/bin/env bash
# 打一个可直接分发的 tar.gz（不含 .git / 虚拟环境 / 用户配置）
#
#   bash tools/make_release.sh           版本号取 git describe
#   bash tools/make_release.sh v0.2.0    指定版本号
# 产物：dist/deckmeido-<版本>.tar.gz
#       dist/deckmeido-<版本>.tar.gz.sha256（也汇总进 dist/SHA256SUMS）
#
# 包内额外带两个文件：
#   VERSION        安装时 install.sh 优先读它写进安装目录
#   RELEASE-NOTES.md  上一 tag 到本次 tag 之间的提交标题
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VER="${1:-$(git -C "$ROOT" describe --tags --always 2>/dev/null || echo dev)}"
VER="${VER#v}"
NAME="deckmeido-${VER}"
OUT="$ROOT/dist"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

# 版本号是 v 开头的 tag 名才去算提交区间；其它（如 describe 出来的 dev 串）只列最近提交
gen_notes() {
  local dest="$1" prev="" range=""
  if git -C "$ROOT" rev-parse -q --verify "refs/tags/v$VER" >/dev/null 2>&1; then
    prev=$(git -C "$ROOT" describe --tags --abbrev=0 "v$VER^" 2>/dev/null || true)
    [ -n "$prev" ] && range="$prev..v$VER"
  fi
  {
    echo "# DeckMeido $VER"
    echo
    echo "> 生成时间：$(date '+%Y-%m-%d %H:%M')"
    [ -n "$prev" ] && echo "> 变更范围：\`$prev\` → \`v$VER\`"
    echo
    if [ -n "$range" ]; then
      echo "## 变更"
      echo
      git -C "$ROOT" log "$range" --no-merges --pretty='- %s' || true
    else
      echo "## 最近提交"
      echo
      git -C "$ROOT" log -n 30 --no-merges --pretty='- %s' || true
    fi
  } > "$dest"
}

mkdir -p "$OUT" "$STAGE/$NAME"
cd "$ROOT"
cp -r server client assets deploy tools plugin requirements.txt README.md install.sh "$STAGE/$NAME/"
# 清掉不该进包的东西
rm -rf "$STAGE/$NAME/server/config.toml" \
       "$STAGE/$NAME/server/sessions" \
       "$STAGE/$NAME/server/uploads" \
       "$STAGE/$NAME/server/static/shots" \
       "$STAGE/$NAME/server/overlay.log" \
       "$STAGE/$NAME/plugin/node_modules"
find "$STAGE/$NAME" -name '__pycache__' -type d -prune -exec rm -rf {} +
chmod +x "$STAGE/$NAME/install.sh"

printf '%s\n' "$VER" > "$STAGE/$NAME/VERSION"
gen_notes "$STAGE/$NAME/RELEASE-NOTES.md"

# 离线依赖包：把 deploy/deps/*.pkg.tar.zst 一起带上。
# install.sh 会优先吃这个目录里的包，装不上再回退联网。目录为空就跳过，
# 不阻断发版——离线包是锦上添花，不该让打包卡住。
if compgen -G "$ROOT/deploy/deps/*.pkg.tar.zst" > /dev/null; then
  mkdir -p "$STAGE/$NAME/deploy/deps"
  cp "$ROOT"/deploy/deps/*.pkg.tar.zst "$STAGE/$NAME/deploy/deps/"
  echo "已并入离线依赖：$(ls "$STAGE/$NAME/deploy/deps" | wc -l) 个包"
fi

tar -C "$STAGE" -czf "$OUT/$NAME.tar.gz" "$NAME"

# 校验和：单文件一份 + 汇总一份，安装前可 sha256sum -c 核对
(
  cd "$OUT"
  sha256sum "$NAME.tar.gz" > "$NAME.tar.gz.sha256"
  sha256sum "$NAME.tar.gz" >> SHA256SUMS.tmp
  # 同一版本重打包时替换旧行，避免汇总文件里堆重复
  grep -v " $NAME.tar.gz\$" SHA256SUMS 2>/dev/null > SHA256SUMS.keep || true
  cat SHA256SUMS.tmp SHA256SUMS.keep > SHA256SUMS
  rm -f SHA256SUMS.tmp SHA256SUMS.keep
)

echo "已生成 $OUT/$NAME.tar.gz"
echo "已生成 $OUT/SHA256SUMS"
