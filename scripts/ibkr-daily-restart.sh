#!/bin/bash
# Daily preventive restart of Gateway + daemon.
#
# Runs once a day via cron (default 04:30 UTC = 00:30 ET, well after US
# market close). Bounces Gateway so IBC does a fresh login, then bounces
# the daemon so it grabs a clean connection. Catches IBC accumulation bugs
# before they cause the symptom we saw (port 4002 closed silently after the
# nightly IBKR-mandated logout).
#
# Belt-and-suspenders with the watchdog (which catches failures within
# minutes); this just prevents most failures from happening in the first
# place by never letting Gateway state accumulate for >24h.
#
# Logs to the same file as the watchdog so all ops activity is in one place.
set -uo pipefail

LOG=${IBKR_OPS_LOG:-/home/trader/ibkr-watchdog.log}
GATEWAY_COMPOSE=${IBKR_GATEWAY_COMPOSE:-/home/trader/ibkr-stack/docker-compose.yml}
GATEWAY_WAIT=${IBKR_GATEWAY_WAIT:-120}

TS() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "$(TS) [daily-restart] $*" >> "$LOG"; }

log "starting"

if ! /usr/bin/docker compose -f "$GATEWAY_COMPOSE" restart ib-gateway >> "$LOG" 2>&1; then
    log "ERROR: docker compose restart returned non-zero; aborting daemon restart"
    exit 1
fi
log "Gateway restart issued; waiting ${GATEWAY_WAIT}s for login"
sleep "$GATEWAY_WAIT"

if /usr/bin/sudo /usr/bin/systemctl restart ibkr-mcp >> "$LOG" 2>&1; then
    log "daemon restart issued"
else
    log "ERROR: systemctl restart ibkr-mcp returned non-zero (sudoers configured?)"
    exit 1
fi

log "complete"
