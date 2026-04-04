#!/usr/bin/env bash
# =============================================================================
# deploy.sh — deploy only changed files to remote server (MD5 comparison)
# Usage:  bash deploy.sh [--env] [--force]
#
#   (no flags)  copy only changed .py files + restart if anything changed
#   --env       also check and copy .env if changed
#   --force     skip MD5 check, copy everything
# =============================================================================
set -euo pipefail

# --- Colors ------------------------------------------------------------------
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
die()   { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }
skip()  { echo -e "  ·  $*  ${YELLOW}(unchanged)${NC}"; }
send()  { echo -e "  ${GREEN}→${NC}  $*  ${GREEN}(updated)${NC}"; }

# --- Config ------------------------------------------------------------------
SSH_ALIAS="scr"
REMOTE_DIR="/app/telemon"
SERVICE_NAME="telemon"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DEPLOY_ENV=false
FORCE=false
for arg in "$@"; do
    case "$arg" in
        --env)   DEPLOY_ENV=true ;;
        --force) FORCE=true ;;
    esac
done

# --- Files to check ----------------------------------------------------------
CORE_FILES=(
    "src/telemon.py"
    "src/chatbot.py"
    "src/diagnostics.py"
    "src/chronic_tracker.py"
    "src/llm_logger.py"
)

echo "============================================="
echo "  Telemon — deploy to $SSH_ALIAS:$REMOTE_DIR"
$FORCE && echo "  Mode: FORCE (skipping MD5 check)" || echo "  Mode: MD5 diff"
echo "============================================="
echo ""

# --- Collect local MD5s in one pass ------------------------------------------
LOCAL_PATHS=()
for f in "${CORE_FILES[@]}"; do
    [[ -f "$SCRIPT_DIR/$f" ]] && LOCAL_PATHS+=("$SCRIPT_DIR/$f")
done
if $DEPLOY_ENV && [[ -f "$SCRIPT_DIR/.env" ]]; then
    LOCAL_PATHS+=("$SCRIPT_DIR/.env")
fi

# md5sum output: "<hash>  <path>"
declare -A LOCAL_MD5
while IFS= read -r line; do
    hash="${line%% *}"
    path="${line##* }"
    LOCAL_MD5["$path"]="$hash"
done < <(md5sum "${LOCAL_PATHS[@]}")

# --- Collect remote MD5s in one SSH call -------------------------------------
# Build list of remote paths
REMOTE_PATHS=()
for f in "${CORE_FILES[@]}"; do
    REMOTE_PATHS+=("$REMOTE_DIR/$(basename "$f")")
done
$DEPLOY_ENV && REMOTE_PATHS+=("$REMOTE_DIR/.env")

# Run md5sum on remote; ignore missing files (2>/dev/null || true)
declare -A REMOTE_MD5
while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    hash="${line%% *}"
    path="${line##* }"
    REMOTE_MD5["$path"]="$hash"
done < <(ssh "$SSH_ALIAS" "md5sum ${REMOTE_PATHS[*]} 2>/dev/null || true")

# --- Compare and deploy ------------------------------------------------------
CHANGED=()

for f in "${CORE_FILES[@]}"; do
    local_path="$SCRIPT_DIR/$f"
    remote_path="$REMOTE_DIR/$(basename "$f")"

    [[ -f "$local_path" ]] || { warn "skipped (not found): $f"; continue; }

    local_hash="${LOCAL_MD5[$local_path]:-}"
    remote_hash="${REMOTE_MD5[$remote_path]:-}"

    if $FORCE || [[ "$local_hash" != "$remote_hash" ]]; then
        scp -q "$local_path" "$SSH_ALIAS:$remote_path"
        send "$f"
        CHANGED+=("$f")
    else
        skip "$f"
    fi
done

# --- .env (optional) ---------------------------------------------------------
if $DEPLOY_ENV; then
    local_path="$SCRIPT_DIR/.env"
    remote_path="$REMOTE_DIR/.env"
    [[ -f "$local_path" ]] || die ".env not found locally"

    local_hash="${LOCAL_MD5[$local_path]:-}"
    remote_hash="${REMOTE_MD5[$remote_path]:-}"

    if $FORCE || [[ "$local_hash" != "$remote_hash" ]]; then
        scp -q "$local_path" "$SSH_ALIAS:$remote_path"
        ssh "$SSH_ALIAS" "chmod 600 $remote_path"
        send ".env"
        CHANGED+=(".env")
    else
        skip ".env"
    fi
fi

# --- Restart only if something changed ---------------------------------------
echo ""
if [[ ${#CHANGED[@]} -eq 0 ]]; then
    info "Nothing changed — skipping restart"
    echo ""
    exit 0
fi

info "Restarting $SERVICE_NAME (${#CHANGED[@]} file(s) updated)..."
ssh "$SSH_ALIAS" "systemctl restart $SERVICE_NAME"
sleep 2

# --- Status ------------------------------------------------------------------
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
echo -e "  Follow logs:   ${GREEN}ssh $SSH_ALIAS 'journalctl -u $SERVICE_NAME -f'${NC}"
echo -e "  LLM log:       ${GREEN}ssh $SSH_ALIAS 'tail -f /var/lib/system-monitor/llm.log'${NC}"
echo -e "  Chronic state: ${GREEN}ssh $SSH_ALIAS 'cat /var/lib/system-monitor/chronic_state.json'${NC}"
echo ""
