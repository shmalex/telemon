#!/usr/bin/env bash
# =============================================================================
# deploy.sh —快速 deploy updated files to remote server without full reinstall
# Usage:  bash deploy.sh [--env] [--all]
#
#   (no flags)  copy Python source files + restart service
#   --env       also copy .env
#   --all       copy everything including diagnostics.py, chatbot.py, etc.
# =============================================================================
set -euo pipefail

# --- Colors ------------------------------------------------------------------
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
die()   { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }

# --- Config ------------------------------------------------------------------
SSH_ALIAS="scr"
REMOTE_DIR="/app/telemon"
SERVICE_NAME="telemon"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DEPLOY_ENV=false
for arg in "$@"; do
    case "$arg" in
        --env) DEPLOY_ENV=true ;;
    esac
done

# --- Validate local files exist ----------------------------------------------
[[ -f "$SCRIPT_DIR/src/telemon.py" ]] || die "src/telemon.py not found"

# --- Files to always deploy --------------------------------------------------
CORE_FILES=(
    "src/telemon.py"
    "src/chatbot.py"
    "src/diagnostics.py"
    "src/chronic_tracker.py"
    "src/llm_logger.py"
)

echo "============================================="
echo "  Telemon — deploy to $SSH_ALIAS:$REMOTE_DIR"
echo "============================================="
echo ""

# --- Copy source files -------------------------------------------------------
info "Copying source files..."
for f in "${CORE_FILES[@]}"; do
    local_path="$SCRIPT_DIR/$f"
    remote_path="$REMOTE_DIR/$(basename "$f")"
    if [[ -f "$local_path" ]]; then
        scp -q "$local_path" "$SSH_ALIAS:$remote_path"
        echo "  → $f"
    else
        warn "  skipped (not found): $f"
    fi
done

# --- Optionally copy .env ----------------------------------------------------
if $DEPLOY_ENV; then
    [[ -f "$SCRIPT_DIR/.env" ]] || die ".env not found"
    info "Copying .env..."
    scp -q "$SCRIPT_DIR/.env" "$SSH_ALIAS:$REMOTE_DIR/.env"
    ssh "$SSH_ALIAS" "chmod 600 $REMOTE_DIR/.env"
    echo "  → .env"
fi

# --- Restart service ---------------------------------------------------------
info "Restarting $SERVICE_NAME..."
ssh "$SSH_ALIAS" "systemctl restart $SERVICE_NAME"
sleep 2

# --- Check status ------------------------------------------------------------
echo ""
echo "============================================="
if ssh "$SSH_ALIAS" "systemctl is-active --quiet $SERVICE_NAME"; then
    info "Service is running"
else
    warn "Service did not start — check logs:"
    echo ""
    ssh "$SSH_ALIAS" "journalctl -u $SERVICE_NAME -n 20 --no-pager"
fi
echo "============================================="
echo ""
echo -e "  Follow logs:  ${GREEN}ssh $SSH_ALIAS 'journalctl -u $SERVICE_NAME -f'${NC}"
echo -e "  LLM log:      ${GREEN}ssh $SSH_ALIAS 'tail -f /var/lib/system-monitor/llm.log'${NC}"
echo -e "  Chronic state:${GREEN}ssh $SSH_ALIAS 'cat /var/lib/system-monitor/chronic_state.json'${NC}"
echo ""
