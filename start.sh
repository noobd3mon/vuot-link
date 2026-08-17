#!/bin/sh
# start.sh — Khởi động Xvfb (display ảo) + x11vnc (VNC server) rồi chạy uvicorn.
# Đợi Xvfb sẵn sàng trước khi khởi Chrome (tránh crash lúc app nhận request đầu).
set -e

echo "[start] Xvfb starting on :99 ..."
Xvfb :99 -screen 0 1280x800x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
XVFB_PID=$!

# Đợi display socket xuất hiện; fail nhanh nếu Xvfb chết.
i=0
while [ $i -lt 50 ]; do
  [ -e /tmp/.X11-unix/X99 ] && break
  if ! kill -0 "$XVFB_PID" 2>/dev/null; then
    echo "[start] Xvfb failed — log:"; cat /tmp/xvfb.log; exit 1
  fi
  sleep 0.1; i=$((i + 1))
done
echo "[start] Xvfb ready"

echo "[start] x11vnc on 127.0.0.1:5900 ..."
x11vnc -display :99 -forever -shared -nopw -localhost -rfbport 5900 >/tmp/x11vnc.log 2>&1 &

echo "[start] uvicorn on :${PORT:-8080}"
exec uvicorn app:app --host 0.0.0.0 --port "${PORT:-8080}"
