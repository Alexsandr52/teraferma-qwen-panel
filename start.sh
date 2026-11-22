#!/usr/bin/env bash
set -euo pipefail

APP_DIR=$(cd "$(dirname "$0")" && pwd)
PANEL_URL=http://127.0.0.1:8787
mkdir -p "$APP_DIR/.local"
chmod 700 "$APP_DIR/.local"

if [[ ${1:-} == --restart ]] && curl -fsS --max-time 2 "$PANEL_URL/api/health" >/dev/null; then
  # The panel's SIGTERM handler can stop the paid GPU. Only restart an idle panel.
  if ! curl -fsS --max-time 3 "$PANEL_URL/api/state" | python3 -c '
import json, sys
state = json.load(sys.stdin)
rental = state["rental"]
saving = any(item.get("status") in {"starting", "creating", "provisioning", "downloading", "downloaded", "saving"}
             for item in state.get("storage", {}).values())
sys.exit(0 if rental.get("confirmed_stopped") is True and not saving else 1)'; then
    echo "Перезапуск отложен. Проверь состояние аренды и сохранения в панели." >&2
    exit 1
  fi
  if command -v systemctl >/dev/null && systemctl --user is-active --quiet teraferma-qwen-panel; then
    systemctl --user stop teraferma-qwen-panel
  else
    pid=$(cat "$APP_DIR/.local/panel.pid")
    listener=$(lsof -nP -a -p "$pid" -iTCP:8787 -sTCP:LISTEN -t)
    owner_dir=$(lsof -a -p "$pid" -d cwd -Fn | sed -n 's/^n//p')
    if [[ "$listener" != "$pid" || "$owner_dir" != "$APP_DIR" ]]; then
      echo "Процесс на порту 8787 не принадлежит этому проекту." >&2
      exit 1
    fi
    kill -TERM "$pid"
  fi
  for _ in $(seq 1 40); do
    if ! curl -fsS --max-time 1 "$PANEL_URL/api/health" >/dev/null 2>&1; then break; fi
    sleep 0.25
  done
  if curl -fsS --max-time 1 "$PANEL_URL/api/health" >/dev/null 2>&1; then
    echo "Предыдущий процесс панели ещё работает." >&2
    exit 1
  fi
fi

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
if [[ $(uname -s) == Darwin ]]; then
  open "$PANEL_URL" >/dev/null 2>&1 || true
else
  xdg-open "$PANEL_URL" >/dev/null 2>&1 || true
fi
echo "$PANEL_URL"
