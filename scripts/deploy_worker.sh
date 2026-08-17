#!/usr/bin/env bash
# =============================================================================
# V3_cursor_Cluster — Worker deploy script
# Usage on each worker machine:
#   WORKER_ID=rtx3050-01 WORKER_LABEL="RTX 3050" WORKER_API_KEY=v3c_xxx \
#   GATEWAY_URL=http://100.90.235.15:8770 \
#   ./deploy_worker.sh
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_NAME="v3cluster-worker"
RUN_USER="${RUN_USER:-${SUDO_USER:-root}}"
APP_HOME="/opt/v3cluster-worker"
VENV="$APP_HOME/venv"

# Required env: WORKER_API_KEY, GATEWAY_URL, WORKER_ID, WORKER_LABEL
: "${WORKER_API_KEY:?WORKER_API_KEY required}"
: "${GATEWAY_URL:?GATEWAY_URL required (e.g. http://100.90.235.15:8770)}"
: "${WORKER_ID:?WORKER_ID required (e.g. rtx3050-01)}"
: "${WORKER_LABEL:?WORKER_LABEL required (e.g. 'RTX 3050 8GB')}"

WORKER_MAX_PARALLEL="${WORKER_MAX_PARALLEL:-2}"
WORKER_GPU_LABEL="${WORKER_GPU_LABEL:-$WORKER_LABEL}"
FFMPEG_BIN="${FFMPEG_BIN:-/usr/bin/ffmpeg}"

if [[ "$EUID" -ne 0 ]] && [[ -z "${SUDO_USER:-}" ]]; then
    echo "Must run as root (or with sudo)"; exit 1
fi

echo "[worker] preparing app home at $APP_HOME"
mkdir -p "$APP_HOME/work"
rsync -a --delete \
    --exclude='.venv' --exclude='__pycache__' --exclude='.git' \
    --exclude='work' --exclude='*.log' \
    "$REPO_ROOT/" "$APP_HOME/app/"

echo "[worker] creating venv at $VENV"
if [[ ! -d "$VENV" ]]; then
    sudo -u "$RUN_USER" python3 -m venv "$VENV"
fi

echo "[worker] installing deps"
sudo -u "$RUN_USER" "$VENV/bin/pip" install -q --upgrade pip
sudo -u "$RUN_USER" "$VENV/bin/pip" install -q -r "$APP_HOME/app/worker/requirements.txt"

echo "[worker] writing .env"
cat >"$APP_HOME/.env" <<EOF
GATEWAY_URL=${GATEWAY_URL}
WORKER_API_KEY=${WORKER_API_KEY}
WORKER_ID=${WORKER_ID}
WORKER_LABEL=${WORKER_LABEL}
WORKER_GPU_LABEL=${WORKER_GPU_LABEL}
WORKER_MAX_PARALLEL=${WORKER_MAX_PARALLEL}
FFMPEG_BIN=${FFMPEG_BIN}
FFPROBE_BIN=${FFPROBE_BIN:-/usr/bin/ffprobe}
WORK_DIR=${APP_HOME}/work
WORKER_HEARTBEAT_INTERVAL=10
WORKER_POLL_INTERVAL=3
EOF
chown "$RUN_USER":"$(id -gn "$RUN_USER")" "$APP_HOME/.env"
chmod 600 "$APP_HOME/.env"

echo "[worker] installing systemd unit"
cat >/etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=V3 Cluster Worker (${WORKER_ID})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=$(id -gn "$RUN_USER")
WorkingDirectory=${APP_HOME}/app
EnvironmentFile=${APP_HOME}/.env
ExecStart=${VENV}/bin/python -m worker.main
Restart=always
RestartSec=5
LimitNOFILE=65536
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
sleep 2
systemctl --no-pager status "$SERVICE_NAME" --lines=15 || true

echo
echo "[worker] ${WORKER_ID} deployed. Check logs with: journalctl -u ${SERVICE_NAME} -f"
