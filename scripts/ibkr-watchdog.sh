#!/bin/bash
# IBKR daemon + Gateway watchdog.
#
# Runs every 5 minutes via cron. Checks the connection chain end-to-end and
# self-heals if anything is broken. Designed to be safe to run on a healthy
# system — does nothing unless a check fails.
#
# Checks, in order of escalation:
#   1. Is Gateway's API port (4002) listening?
#        - if no -> restart Gateway container + daemon
#   2. Does the daemon's HTTP /healthz endpoint respond?
#        - if no -> restart daemon
#   3. Does /healthz report ibkr_connected=true?
#        - if no -> restart daemon (it'll reconnect to Gateway on startup)
#
# Logs to /home/trader/ibkr-watchdog.log by default. Override via
# IBKR_WATCHDOG_LOG env var. Other paths overridable too — see env block.
#
# Lock file prevents overlap if a previous run is still healing.
set -uo pipefail

# --- config (override via env if your layout differs) ---------------------
LOG=${IBKR_WATCHDOG_LOG:-/home/trader/ibkr-watchdog.log}
ENV_FILE=${IBKR_WATCHDOG_ENV_FILE:-/home/trader/ibkr-mcp-server/.env}
GATEWAY_COMPOSE=${IBKR_WATCHDOG_GATEWAY_COMPOSE:-/home/trader/ibkr-stack/docker-compose.yml}
HEALTHZ_URL=${IBKR_WATCHDOG_HEALTHZ_URL:-http://127.0.0.1:8765/healthz}
GATEWAY_PORT=${IBKR_WATCHDOG_GATEWAY_PORT:-4002}
GATEWAY_RESTART_WAIT=${IBKR_WATCHDOG_GATEWAY_WAIT:-90}
DAEMON_RESTART_WAIT=${IBKR_WATCHDOG_DAEMON_WAIT:-5}
LOCK_FILE=/tmp/ibkr-watchdog.lock

# --- single-run guard -----------------------------------------------------
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "$(date -u +%FT%TZ) SKIP: previous watchdog run still in progress" >> "$LOG"
    exit 0
fi

TS() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "$(TS) $*" >> "$LOG"; }

# Pull bearer token if the daemon is configured to require one.
TOKEN=""
if [ -r "$ENV_FILE" ]; then
    TOKEN=$(grep -E '^MCP_AUTH_TOKEN=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
fi

restart_gateway() {
    log "ACTION: restarting Gateway container"
    if /usr/bin/docker compose -f "$GATEWAY_COMPOSE" restart ib-gateway >> "$LOG" 2>&1; then
        log "WAIT: ${GATEWAY_RESTART_WAIT}s for Gateway login"
        sleep "$GATEWAY_RESTART_WAIT"
    else
        log "FAIL: docker compose restart returned non-zero"
    fi
}

restart_daemon() {
    log "ACTION: restarting ibkr-mcp daemon"
    if /usr/bin/sudo /usr/bin/systemctl restart ibkr-mcp >> "$LOG" 2>&1; then
        log "WAIT: ${DAEMON_RESTART_WAIT}s for daemon"
        sleep "$DAEMON_RESTART_WAIT"
    else
        log "FAIL: systemctl restart ibkr-mcp returned non-zero (sudoers configured?)"
    fi
}

# --- check 1: Gateway port listening --------------------------------------
if ! ss -tln 2>/dev/null | grep -q ":$GATEWAY_PORT "; then
    log "FAIL: Gateway port $GATEWAY_PORT not listening"
    restart_gateway
    restart_daemon
    exit 0
fi

# --- check 2: daemon HTTP responding --------------------------------------
declare -a CURL_HEADERS=()
if [ -n "$TOKEN" ]; then
    CURL_HEADERS+=(-H "Authorization: Bearer $TOKEN")
fi
RESP=$(/usr/bin/curl -fsS -m 5 "${CURL_HEADERS[@]}" "$HEALTHZ_URL" 2>/dev/null || true)
if [ -z "$RESP" ]; then
    log "FAIL: daemon HTTP unresponsive at $HEALTHZ_URL"
    restart_daemon
    exit 0
fi

# --- check 3: daemon reports IBKR connection healthy ----------------------
if echo "$RESP" | grep -q '"ibkr_connected"[[:space:]]*:[[:space:]]*false'; then
    log "FAIL: daemon up but ibkr_connected=false; restarting daemon to force reconnect"
    restart_daemon
    exit 0
fi

# --- proof-of-life log once per hour (when ALL is well) -------------------
# Cron fires every 5 minutes; log on the 0-4 minute slot so we get exactly
# one OK line per hour rather than 12.
MIN=$(date +%-M)
if [ "$MIN" -lt 5 ]; then
    log "OK: chain healthy"
fi
