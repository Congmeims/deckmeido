#!/usr/bin/env bash
# 汇总离线依赖包到 deploy/deps/，供 make_release.sh 打进发布包。
#
#   bash tools/fetch_offline_deps.sh
#
# 必须在 SteamOS 上、并且已联网时执行：它要用 pacman 把包下载到本地
# （--downloadonly，不真正安装），同时把依赖树一并拉下来，否则
# install.sh 在断网机器上 pacman -U 会因为缺依赖失败。
#
# 注意：SteamOS 换镜像（A/B 分区）后 holo 仓库的版本会变，旧包可能
# 比仓库还旧。每次发版前重跑一遍这个脚本，包才跟得上。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/deploy/deps"

# 与 install.sh 里的 DEPS_PACMAN 保持一致
PKGS=(espeak-ng ydotool wtype fakeroot)

mkdir -p "$OUT"

echo "==> 刷新仓库索引"
sudo pacman -Sy

echo "==> 下载依赖树（不安装）"
# --downloadonly 只下载；--needed 避免已装过的版本被当成「不需要下载」
sudo pacman -S --needed --noconfirm --downloadonly \
  --cachedir "$OUT" "${PKGS[@]}"

# pacman 的 cachedir 会在里面再建一层 pkg/，摊平到 deploy/deps 根目录，
# install.sh 只按 deploy/deps/*.pkg.tar.zst 一层匹配
if [ -d "$OUT/pkg" ]; then
  find "$OUT/pkg" -maxdepth 1 -name '*.pkg.tar.zst' -exec mv -f {} "$OUT/" \;
  rmdir "$OUT/pkg" 2>/dev/null || true
fi

# 校验：至少核心包在，且能用 zstd 解开
echo
bad=0
for p in "${PKGS[@]}"; do
  hit=$(find "$OUT" -maxdepth 1 -name "$p-*.pkg.tar.zst" | head -n1)
  if [ -z "$hit" ]; then
    echo "[!] 缺少 $p"
    bad=1
    continue
  fi
  zstd -t "$hit" >/dev/null 2>&1 || { echo "[!] $hit 损坏"; bad=1; }
  echo "    $p → $(basename "$hit")"
done

echo
echo "共 $(find "$OUT" -maxdepth 1 -name '*.pkg.tar.zst' | wc -l) 个包，位置 $OUT"
[ "$bad" = 0 ] || { echo "[!] 有包缺失或损坏，检查上面的输出"; exit 1; }
echo "下一步：bash tools/make_release.sh <版本号>"
