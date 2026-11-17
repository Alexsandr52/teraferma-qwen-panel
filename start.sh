#!/usr/bin/env bash
set -euo pipefail

APP_DIR=$(cd "$(dirname "$0")" && pwd)
PANEL_URL=http://127.0.0.1:8787
mkdir -p "$APP_DIR/.local"
chmod 700 "$APP_DIR/.local"

if ! curl -fsS --max-time 2 "$PANEL_URL/api/health" >/dev/null 2>&1; then
  if command -v systemd-run >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
    systemd-run --user --collect --unit=teraferma-qwen-panel \
      --property="WorkingDirectory=$APP_DIR" \
      --property=TimeoutStopSec=240s \
      python3 "$APP_DIR/app.py" >/dev/null
  else
    nohup python3 "$APP_DIR/app.py" >> "$APP_DIR/.local/panel.log" 2>&1 < /dev/null &
    echo $! > "$APP_DIR/.local/panel.pid"
  fi
  for _ in $(seq 1 40); do
    if curl -fsS --max-time 2 "$PANEL_URL/api/health" >/dev/null 2>&1; then
      break
    fi
    sleep 0.25
  done
fi

if ! curl -fsS --max-time 2 "$PANEL_URL/api/health" >/dev/null 2>&1; then
  echo "Панель не запустилась. Смотри journalctl --user -u teraferma-qwen-panel или $APP_DIR/.local/panel.log" >&2
  exit 1
fi
xdg-open "$PANEL_URL" >/dev/null 2>&1 || true
echo "$PANEL_URL"
