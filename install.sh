#!/usr/bin/env bash
# =============================================================================
# install.sh — deploy telemon as a systemd service
# Usage:  sudo bash install.sh
# =============================================================================
set -euo pipefail

# --- Colors ------------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
die()   { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }

# --- Must run as root --------------------------------------------------------
[[ $EUID -eq 0 ]] || die "Run as root:  sudo bash install.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DEPLOY_DIR="/app/telemon"
STATE_DIR="/var/lib/system-monitor"
SERVICE_NAME="telemon"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

echo "============================================="
echo "  Telemon — installation"
echo "============================================="
echo ""

# --- Validate .env -----------------------------------------------------------
[[ -f "$SCRIPT_DIR/.env" ]] || die ".env not found. Copy .env.example → .env and fill in TELEGRAM_BOT_TOKEN."

TOKEN_LINE=$(grep -E "^TELEGRAM_BOT_TOKEN=.+" "$SCRIPT_DIR/.env" || true)
[[ -n "$TOKEN_LINE" ]] || warn "TELEGRAM_BOT_TOKEN is empty — Telegram messages will not be sent"

# --- Python dependencies -----------------------------------------------------
PIP_FLAGS="--break-system-packages --ignore-installed typing-extensions --quiet"

info "Installing core Python dependencies..."
python3 -m pip install matplotlib psutil requests python-dotenv $PIP_FLAGS

# Chatbot dependencies — only if ANTHROPIC_API_KEY is configured
if grep -qE "^ANTHROPIC_API_KEY=.+" "$SCRIPT_DIR/.env" 2>/dev/null; then
    info "Installing chatbot dependencies (langchain, langchain-anthropic)..."
    python3 -m pip install langchain langchain-anthropic $PIP_FLAGS
else
    info "ANTHROPIC_API_KEY not set — skipping chatbot dependencies"
fi

# --- Directories -------------------------------------------------------------
info "Creating directories..."
mkdir -p "$DEPLOY_DIR"
mkdir -p "$STATE_DIR"

# --- Deploy files ------------------------------------------------------------
info "Deploying files to $DEPLOY_DIR ..."
cp "$SCRIPT_DIR/src/telemon.py" "$DEPLOY_DIR/telemon.py"
cp "$SCRIPT_DIR/src/chatbot.py" "$DEPLOY_DIR/chatbot.py"
cp "$SCRIPT_DIR/.env"                  "$DEPLOY_DIR/.env"
chmod 600 "$DEPLOY_DIR/.env"          # protect credentials from other users

# --- systemd unit ------------------------------------------------------------
info "Writing systemd unit $SERVICE_FILE ..."
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Telemon — Telegram alerts for disk, CPU, RAM, services, containers
After=network-online.target docker.service
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 $DEPLOY_DIR/telemon.py
WorkingDirectory=$DEPLOY_DIR
EnvironmentFile=$DEPLOY_DIR/.env
Restart=always
RestartSec=10
User=root

[Install]
WantedBy=multi-user.target
EOF

# --- Enable & start ----------------------------------------------------------
info "Enabling and starting $SERVICE_NAME ..."
systemctl daemon-reload
systemctl enable  "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

# Give it a moment to start
sleep 2

# --- Result ------------------------------------------------------------------
echo ""
echo "============================================="
if systemctl is-active --quiet "$SERVICE_NAME"; then
    info "Service is running"
else
    warn "Service did not start — check logs below"
fi
echo "============================================="
echo ""
systemctl status "$SERVICE_NAME" --no-pager -l
echo ""
echo -e "  Follow logs:    ${GREEN}journalctl -u $SERVICE_NAME -f${NC}"
echo -e "  Stop service:   ${GREEN}systemctl stop $SERVICE_NAME${NC}"
echo -e "  Config file:    ${GREEN}$DEPLOY_DIR/.env${NC}"
echo ""
