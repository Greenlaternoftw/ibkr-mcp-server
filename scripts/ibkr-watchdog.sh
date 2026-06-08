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
# Phone alerts (ntfy.sh):
#   The daemon itself sends alerts on IBKR connection drops, but when the
#   daemon is *itself* wedged or down, it can't notify anyone. So this
#   script doubles as the alarm for those cases. To avoid alert spam, we
#   only fire on state transitions (down -> up, up -> down), tracked via
#   /tmp/ibkr-watchdog.last-state.
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
# GATEWAY_PORT: if not pinned explicitly via IBKR_WATCHDOG_GATEWAY_PORT,
# we DERIVE it from IBKR_PORT in the daemon's .env (resolved below, after
# read_env is defined). This is the cornerstone fix for the "watchdog
# restarts a healthy live Gateway every 5 minutes" bug: the old hardcoded
# 4002 default is the PAPER Gateway port, so the moment anyone flipped to
# live (port 4001, or 7496/7497 for the TWS image) the watchdog found 4002
# dead on every tick and helpfully bounced a perfectly-healthy Gateway --
# forcing a re-login + 2FA push every 5 minutes, forever. Deriving from
# the same .env the daemon connects through keeps them in lockstep.
GATEWAY_PORT=${IBKR_WATCHDOG_GATEWAY_PORT:-}
GATEWAY_RESTART_WAIT=${IBKR_WATCHDOG_GATEWAY_WAIT:-90}
DAEMON_RESTART_WAIT=${IBKR_WATCHDOG_DAEMON_WAIT:-5}
LOCK_FILE=/tmp/ibkr-watchdog.lock
STATE_FILE=${IBKR_WATCHDOG_STATE_FILE:-/tmp/ibkr-watchdog.last-state}

# HEALTHZ_URL: if not pinned explicitly, derive from MCP_BIND_HOST /
# MCP_BIND_PORT in .env so we always probe the same address the daemon
# listens on. Hardcoding 127.0.0.1 used to silently break the moment
# anyone bound the daemon to a non-localhost interface (e.g. Tailscale)
# -- watchdog would 'connection refused' every 5 min and helpfully
# restart the perfectly-healthy daemon. We resolve the env vars below,
# AFTER read_env is defined, in the "--- read env ---" block.
HEALTHZ_URL=${IBKR_WATCHDOG_HEALTHZ_URL:-}

# --- single-run guard -----------------------------------------------------
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "$(date -u +%FT%TZ) SKIP: previous watchdog run still in progress" >> "$LOG"
    exit 0
fi

TS() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "$(TS) $*" >> "$LOG"; }

# --- read env -------------------------------------------------------------
# Pull bearer token + ntfy config straight from .env so the watchdog
# stays in sync with the daemon without needing its own config.
TOKEN=""
NOTIFY_ENABLED_VAL=""
NTFY_URL_VAL=""
NTFY_TOPIC_VAL=""
read_env() {
    local key="$1"
    grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'"
}
if [ -r "$ENV_FILE" ]; then
    TOKEN=$(read_env MCP_AUTH_TOKEN)
    NOTIFY_ENABLED_VAL=$(read_env NOTIFY_ENABLED)
    NTFY_URL_VAL=$(read_env NTFY_URL)
    NTFY_TOPIC_VAL=$(read_env NTFY_TOPIC)
    BIND_HOST=$(read_env MCP_BIND_HOST)
    BIND_PORT=$(read_env MCP_BIND_PORT)
    IBKR_PORT_VAL=$(read_env IBKR_PORT)
    IBKR_IS_PAPER_VAL=$(read_env IBKR_IS_PAPER)
fi

# Live mode is anything where IBKR_IS_PAPER is not strictly "true". Paper
# mode auto-logs in instantly; live mode requires human 2FA approval. The
# watchdog must behave very differently in the two modes:
#   - paper: aggressive restart on any port-down (safe, no human in loop)
#   - live:  NEVER auto-restart Gateway. A port-down moment is almost
#            always either (a) the operator hasn't approved 2FA yet, or
#            (b) Gateway is in a logout/restart cycle. Auto-restarting
#            Gateway forces a NEW 2FA push, which the operator may also
#            miss, which triggers another restart, ad infinitum -- the
#            "5-minute clock-aligned 2FA storm" we hit on 2026-06-05.
# In live mode we only restart the DAEMON (it's stateless and won't push
# a 2FA prompt). Gateway is operator-owned.
IS_LIVE_MODE="false"
case "${IBKR_IS_PAPER_VAL,,}" in
    ""|"true"|"yes"|"1") IS_LIVE_MODE="false" ;;
    *) IS_LIVE_MODE="true" ;;
esac
NTFY_URL_VAL=${NTFY_URL_VAL:-https://ntfy.sh}

# Resolve HEALTHZ_URL from daemon config if the cron didn't pin it. This
# is the cornerstone fix for the "watchdog kills healthy daemon" bug --
# the watchdog now follows the daemon's actual bind address instead of
# guessing 127.0.0.1.
if [ -z "$HEALTHZ_URL" ]; then
    HEALTHZ_URL="http://${BIND_HOST:-127.0.0.1}:${BIND_PORT:-8765}/healthz"
fi

# Resolve GATEWAY_PORT from the daemon's IBKR_PORT if the cron didn't pin
# it explicitly. Follows whatever port the daemon actually connects to:
#   4002 = Gateway paper   4001 = Gateway live
#   7497 = TWS paper       7496 = TWS live
# Falls back to 4002 only if .env has no IBKR_PORT at all (legacy default).
# This prevents the watchdog from bouncing a healthy live Gateway because
# it was hardcoded to look for the paper port.
if [ -z "$GATEWAY_PORT" ]; then
    GATEWAY_PORT="${IBKR_PORT_VAL:-4002}"
fi

# --- ntfy helper (failure-silent) -----------------------------------------
# Args: title, message, priority (1-5), tags (comma list)
# We tolerate any failure here — alerts must never break the watchdog's
# self-healing path.
ntfy() {
    local title="$1"
    local msg="$2"
    local prio="${3:-4}"
    local tags="${4:-}"

    [ "${NOTIFY_ENABLED_VAL,,}" = "true" ] || return 0
    [ -n "$NTFY_TOPIC_VAL" ] || return 0

    /usr/bin/curl -fsS -m 3 \
        -H "Title: $title" \
        -H "Priority: $prio" \
        -H "Tags: $tags" \
        -d "$msg" \
        "${NTFY_URL_VAL%/}/${NTFY_TOPIC_VAL}" >/dev/null 2>&1 || true
}

# --- state transition tracking --------------------------------------------
# State file holds one token: "ok", "gateway_down", "daemon_down", "ibkr_down".
# We only send a "down" alert when the state CHANGES (so /healthz being
# wedged for 6 hours produces 1 alert, not 72), and we send a "recovered"
# alert when state goes back to "ok".
LAST_STATE=$(cat "$STATE_FILE" 2>/dev/null || echo "unknown")

set_state() {
    local new="$1"
    echo "$new" > "$STATE_FILE" 2>/dev/null || true
}

on_transition_to_failure() {
    local kind="$1"   # one of: gateway_down, daemon_down, ibkr_down
    local title msg
    case "$kind" in
        gateway_down)
            title="IBKR Gateway port down"
            if [ "$IS_LIVE_MODE" = "true" ]; then
                msg="Gateway port $GATEWAY_PORT not listening (LIVE mode). NOT auto-restarting -- needs 2FA. Restart via: cd /home/trader/ibkr-stack && docker compose restart ib-gateway, then approve 2FA on phone."
            else
                msg="Gateway port $GATEWAY_PORT not listening on the VPS. Restarting Gateway + daemon. Check ibkr-watchdog.log if this repeats."
            fi
            ;;
        daemon_down)
            title="IBKR daemon HTTP wedged"
            msg="Daemon /healthz not responding at $HEALTHZ_URL. Restarting daemon. iPhone/Claude MCP will be unreachable for ~10s."
            ;;
        ibkr_down)
            title="IBKR connection unhealthy"
            msg="Daemon reports ibkr_connected=false. Restarting daemon to force a fresh login to Gateway."
            ;;
        *)
            return 0
            ;;
    esac
    # Only alert on first detection of this failure mode.
    if [ "$LAST_STATE" != "$kind" ]; then
        ntfy "$title" "$msg" 4 "warning,rotating_light"
    fi
    set_state "$kind"
}

on_transition_to_ok() {
    if [ "$LAST_STATE" != "ok" ] && [ "$LAST_STATE" != "unknown" ]; then
        ntfy "IBKR chain recovered" \
            "Watchdog: connection chain healthy again (was: $LAST_STATE)." \
            2 "white_check_mark"
    fi
    set_state "ok"
}

# --- self-heal actions ----------------------------------------------------
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
# In live mode we DO NOT auto-restart Gateway -- doing so triggers a new
# 2FA push, which if missed cascades into the 5-min restart storm we hit
# on 2026-06-05. The daemon's own persistent reconnect loop (10s
# interval, 10 min ceiling) handles transient Gateway drops; if Gateway
# is truly down, the operator must approve 2FA on their phone anyway,
# so auto-restart adds nothing but noise. We just alert and exit.
if ! ss -tln 2>/dev/null | grep -q ":$GATEWAY_PORT "; then
    log "FAIL: Gateway port $GATEWAY_PORT not listening"
    on_transition_to_failure gateway_down
    if [ "$IS_LIVE_MODE" = "true" ]; then
        log "SKIP: live mode -- Gateway restart needs 2FA, operator owns recovery"
        exit 0
    fi
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
    on_transition_to_failure daemon_down
    restart_daemon
    exit 0
fi

# --- check 3: daemon reports IBKR connection healthy ----------------------
if echo "$RESP" | grep -q '"ibkr_connected"[[:space:]]*:[[:space:]]*false'; then
    log "FAIL: daemon up but ibkr_connected=false; restarting daemon to force reconnect"
    on_transition_to_failure ibkr_down
    restart_daemon
    exit 0
fi

# --- all green ------------------------------------------------------------
on_transition_to_ok

# --- proof-of-life log once per hour (when ALL is well) -------------------
# Cron fires every 5 minutes; log on the 0-4 minute slot so we get exactly
# one OK line per hour rather than 12.
MIN=$(date +%-M)
if [ "$MIN" -lt 5 ]; then
    log "OK: chain healthy"
fi
