#!/bin/sh
# 启动 GPIO 守护进程 + WebUI；收到 TERM 时让守护进程优雅退出（会把 LED 驱动 bind 回去）
set -e
mkdir -p "$FAN_DATA/config"

/app/fanctl &
FANPID=$!

python3 /app/web/server.py &
WEBPID=$!

term() {
  kill -TERM "$FANPID" 2>/dev/null || true
  kill -TERM "$WEBPID" 2>/dev/null || true
  wait "$FANPID" 2>/dev/null || true
  exit 0
}
trap term TERM INT

wait "$WEBPID"
