#!/usr/bin/env bash
# =============================================================================
# V3_cursor_Cluster — Gateway deploy script
# Usage:
#   ./deploy_gateway.sh                          # install + start
#   ./deploy_gateway.sh --user sj88              # run as specific user
#   ./deploy_gateway.sh --stop                   # stop
#   ./deploy_gateway.sh --restart                # restart
#   ./deploy_gateway.sh --status                 # show status
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_NAME="v3cluster-gateway"
RUN_USER="${RUN_USER:-${SUDO_USER:-root}}"
APP_PORT="${GATEWAY_PORT:-8770}"
APP_HOME="/opt/v3cluster"
VENV="$APP_HOME/venv"

stop() {
    echo "[gateway] stopping $SERVICE_NAME"
    systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    echo "[gateway] stopped"
}

start() {
    if [[ -f "$REPO_ROOT/.env" ]]; then
        # shellcheck disable=SC1091
        set -a; source "$REPO_ROOT/.env"; set +a
    fi
    : "${ADMIN_API_KEY:?ADMIN_API_KEY must be set in .env}"
    : "${DATABASE_URL:?DATABASE_URL must be set in .env}"

    echo "[gateway] preparing app home at $APP_HOME"
    mkdir -p "$APP_HOME" "$APP_HOME/storage"
    rsync -a --delete \
        --exclude='.venv' --exclude='__pycache__' --exclude='.git' \
        --exclude='storage' --exclude='*.log' \
        "$REPO_ROOT/" "$APP_HOME/app/"

    echo "[gateway] creating venv at $VENV"
    if [[ ! -d "$VENV" ]]; then
        sudo -u "$RUN_USER" python3 -m venv "$VENV"
    fi

    echo "[gateway] installing deps"
    sudo -u "$RUN_USER" "$VENV/bin/pip" install -q --upgrade pip
    sudo -u "$RUN_USER" "$VENV/bin/pip" install -q -r "$APP_HOME/app/gateway/requirements.txt"

    echo "[gateway] writing .env to $APP_HOME/.env"
    cp "$REPO_ROOT/.env" "$APP_HOME/.env"
    chown "$RUN_USER":"$(id -gn "$RUN_USER")" "$APP_HOME/.env"
    chmod 600 "$APP_HOME/.env"

    echo "[gateway] installing systemd unit"
    cat >/etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=V3 Cluster Gateway (green screen render cluster)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=$(id -gn "$RUN_USER")
WorkingDirectory=${APP_HOME}/app
EnvironmentFile=${APP_HOME}/.env
ExecStart=${VENV}/bin/python -m uvicorn gateway.app:app --host 0.0.0.0 --port ${APP_PORT} --workers 2
Restart=always
RestartSec=3
LimitNOFILE=65536
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

[Install]
WantedBy=multi-user.target
EOF

    echo "[gateway] installing DB schema"
    sudo -u "$RUN_USER" DATABASE_URL="$DATABASE_URL" bash "$APP_HOME/app/scripts/install_db.sh"

    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    systemctl restart "$SERVICE_NAME"
    sleep 2
    systemctl --no-pager status "$SERVICE_NAME" --lines=20 || true

    echo
    echo "[gateway] DEPLOYED. Health check:"
    sleep 1
    curl -sS "http://127.0.0.1:${APP_PORT}/api/v1/health" | python3 -m json.tool || echo "  (gateway not responding yet — check journalctl)"
}

case "${1:-start}" in
    --stop|stop) stop ;;
    --restart|restart) stop; sleep 1; start ;;
    --status|status) systemctl --no-pager status "$SERVICE_NAME" --lines=30 ;;
    *) start ;;
esac
