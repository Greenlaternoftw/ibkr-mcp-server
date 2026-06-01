#!/bin/bash
# One-shot installer for the IBKR ops scripts (watchdog + daily restart).
#
# Idempotent — safe to re-run. Will:
#   1. chmod +x the scripts
#   2. Add a sudoers rule so cron can systemctl restart ibkr-mcp without password
#   3. Add two cron entries (skipping any that already exist)
#   4. Print the resulting crontab for verification
#
# Run on the VPS:
#   bash /home/trader/ibkr-mcp-server/scripts/install-ops.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WATCHDOG="$SCRIPT_DIR/ibkr-watchdog.sh"
DAILY="$SCRIPT_DIR/ibkr-daily-restart.sh"

# 1. Make scripts executable
chmod +x "$WATCHDOG" "$DAILY"
echo "[1/4] scripts chmod +x"

# 2. Sudoers rule so the watchdog can restart the daemon without prompting
SUDOERS_FILE=/etc/sudoers.d/ibkr-ops
SUDOERS_RULE="$USER ALL=NOPASSWD: /usr/bin/systemctl restart ibkr-mcp"

if [ -f "$SUDOERS_FILE" ] && grep -qF "$SUDOERS_RULE" "$SUDOERS_FILE"; then
    echo "[2/4] sudoers rule already present"
else
    echo "$SUDOERS_RULE" | sudo tee "$SUDOERS_FILE" >/dev/null
    sudo chmod 0440 "$SUDOERS_FILE"
    # Validate the file before continuing — broken sudoers locks you out.
    sudo visudo -cf "$SUDOERS_FILE" >/dev/null
    echo "[2/4] sudoers rule installed at $SUDOERS_FILE"
fi

# 3. Cron entries — additive, skip if already present
CURRENT_CRONTAB=$(crontab -l 2>/dev/null || true)
WATCHDOG_LINE="*/5 * * * * $WATCHDOG"
DAILY_LINE="30 4 * * * $DAILY"

ADDITIONS=""
if echo "$CURRENT_CRONTAB" | grep -qF "ibkr-watchdog.sh"; then
    echo "[3/4] watchdog cron entry already present"
else
    ADDITIONS+="$WATCHDOG_LINE"$'\n'
fi
if echo "$CURRENT_CRONTAB" | grep -qF "ibkr-daily-restart.sh"; then
    echo "[3/4] daily-restart cron entry already present"
else
    ADDITIONS+="$DAILY_LINE"$'\n'
fi

if [ -n "$ADDITIONS" ]; then
    # If existing crontab doesn't end with a newline, add one.
    if [ -n "$CURRENT_CRONTAB" ] && [ "${CURRENT_CRONTAB: -1}" != $'\n' ]; then
        CURRENT_CRONTAB+=$'\n'
    fi
    printf '%s%s' "$CURRENT_CRONTAB" "$ADDITIONS" | crontab -
    echo "[3/4] cron entries added"
fi

# 4. Verify
echo "[4/4] current crontab:"
crontab -l | sed 's/^/    /'

echo
echo "Done. Ops scripts installed. Activity logs will appear in:"
echo "    /home/trader/ibkr-watchdog.log"
echo
echo "Tail in real-time with:"
echo "    tail -f /home/trader/ibkr-watchdog.log"
echo
echo "To disable later:"
echo "    crontab -e          # remove the two ibkr-* lines"
echo "    sudo rm $SUDOERS_FILE"
