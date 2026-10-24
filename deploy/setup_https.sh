#!/usr/bin/env bash
# DeckMeido HTTPS 初始化：mkcert 本地 CA + 局域网证书
# 用途：手机浏览器访问时不再提示「不安全」。证书只对本机与当前局域网 IP 有效，
# 换了 WiFi / IP 变了要重新执行一次。
set -euo pipefail

INSTALL_DIR="${DECKMEIDO_DIR:-$HOME/deckmeido}"
cd "$INSTALL_DIR"
mkdir -p bin server/certs

if [ ! -x bin/mkcert ]; then
  echo "下载 mkcert ..."
  curl -fL -o bin/mkcert https://github.com/FiloSottile/mkcert/releases/download/v1.4.4/mkcert-v1.4.4-linux-amd64
  chmod +x bin/mkcert
fi

export CAROOT="$INSTALL_DIR/server/certs"
# Deck 的系统分区只读，装进本机信任库可能失败，无妨：CA 主要给手机安装
bin/mkcert -install || true

> /tmp/hosts.txt
echo localhost >> /tmp/hosts.txt
echo 127.0.0.1 >> /tmp/hosts.txt
ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 >> /tmp/hosts.txt

cd server/certs
"$INSTALL_DIR/bin/mkcert" -cert-file cert.pem -key-file key.pem $(cat /tmp/hosts.txt)

# 根证书放到静态目录，手机浏览器直接下载安装
cp "$CAROOT/rootCA.pem" "$INSTALL_DIR/server/static/ca.crt"
echo "HTTPS 证书已生成。手机访问一次 http://<deck-ip>:18765/ca.crt 安装根证书即可。"
