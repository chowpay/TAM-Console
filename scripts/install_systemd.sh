#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/tam-console}"
SERVICE_NAME="tam-console.service"

if [[ ! -f "$APP_DIR/app.py" ]]; then
  echo "Expected app.py under $APP_DIR. Set APP_DIR=/path/to/tam-console if needed." >&2
  exit 1
fi

sudo install -o root -g root -m 0644 "$APP_DIR/systemd/$SERVICE_NAME" "/etc/systemd/system/$SERVICE_NAME"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

echo "Installed $SERVICE_NAME"
echo "Start with: sudo systemctl start $SERVICE_NAME"
echo "Logs: sudo journalctl -u $SERVICE_NAME -f"
