#!/usr/bin/env bash
# DeckMate HTTPS 初始化：mkcert 本地 CA + 局域网证书
set -e
cd ~/deckmate
mkdir -p bin server/certs
if [ ! -x bin/mkcert ]; then
  curl -L -o bin/mkcert https://github.com/FiloSottile/mkcert/releases/download/v1.4.4/mkcert-v1.4.4-linux-amd64
  chmod +x bin/mkcert
fi
export CAROOT="$HOME/deckmate/server/certs"
# 装进 Deck 本机信任库（系统只读分区会失败，无所谓，CA 主要给手机装）
bin/mkcert -install || true
cd server/certs
~/deckmate/bin/mkcert -cert-file cert.pem -key-file key.pem \
  localhost 127.0.0.1 192.168.124.14
# 根证书放到静态目录，手机浏览器直接下载安装
cp "$HOME/deckmate/server/certs/rootCA.pem" "$HOME/deckmate/server/static/ca.crt"
echo HTTPS_SETUP_DONE
