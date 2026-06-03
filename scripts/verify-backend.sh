#!/usr/bin/env bash
# Backend verification battery for the IBKR Command Center daemon.
# Run on the VPS. Auto-discovers the daemon URL from MCP_BIND_HOST /
# MCP_BIND_PORT in .env (so it works whether you bind to localhost or
# a Tailscale interface). Override with HOST= or ENV_FILE= env vars.
#
# Each numbered test prints PASS/FAIL + the relevant evidence.
# Exits 0 even on partial fail so the full battery always completes.

set -u
ENV_FILE="${ENV_FILE:-$HOME/ibkr-mcp-server/.env}"
BIND_HOST="$(grep ^MCP_BIND_HOST "$ENV_FILE" 2>/dev/null | cut -d= -f2)"
BIND_PORT="$(grep ^MCP_BIND_PORT "$ENV_FILE" 2>/dev/null | cut -d= -f2)"
[ -z "$BIND_HOST" ] && BIND_HOST="127.0.0.1"
[ -z "$BIND_PORT" ] && BIND_PORT="8765"
HOST="${HOST:-http://${BIND_HOST}:${BIND_PORT}}"
echo "Driving daemon at: $HOST"
TOKEN="$(grep ^MCP_AUTH_TOKEN "$ENV_FILE" | cut -d= -f2)"
AUTH="Authorization: Bearer $TOKEN"
PIN="$(grep ^CHAT_PIN "$ENV_FILE" | cut -d= -f2)"
PASS=0
FAIL=0
TMPDIR=$(mktemp -d)

step() { echo; echo "═══ $1 ═══"; }
ok()   { echo "  PASS: $*"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL: $*"; FAIL=$((FAIL+1)); }
probe(){ echo "  🔍   $*"; }

# 1. healthz (no auth)
step "1. /healthz (no auth)"
code=$(curl -s -o /dev/null -w '%{http_code}' "$HOST/healthz")
[ "$code" = "200" ] && ok "200 returned" || bad "expected 200, got $code"

# 2. unauth /chat/api/health → 401
step "2. /chat/api/health WITHOUT bearer → 401"
code=$(curl -s -o /dev/null -w '%{http_code}' "$HOST/chat/api/health")
[ "$code" = "401" ] && ok "401 as expected" || bad "expected 401, got $code"

# 3. authed /chat/api/health → 200 + chat_enabled
step "3. /chat/api/health WITH bearer"
out=$(curl -s -H "$AUTH" "$HOST/chat/api/health")
echo "$out" | jq . 2>/dev/null > "$TMPDIR/health.json"
chat_en=$(jq -r .chat_enabled "$TMPDIR/health.json" 2>/dev/null)
has_key=$(jq -r .has_api_key "$TMPDIR/health.json" 2>/dev/null)
model=$(jq -r .model "$TMPDIR/health.json" 2>/dev/null)
tools=$(jq -r .tool_count "$TMPDIR/health.json" 2>/dev/null)
echo "  body: chat_enabled=$chat_en has_api_key=$has_key model=$model tool_count=$tools"
[ "$chat_en" = "true" ] && [ "$has_key" = "true" ] && ok "chat ready" || bad "chat not ready"
[ -n "$tools" ] && [ "$tools" != "null" ] && [ "$tools" -gt 0 ] && ok "$tools tools registered"

# 4. PIN status (unauth — discovery endpoint)
step "4. /chat/api/pin/status (no auth, public)"
out=$(curl -s "$HOST/chat/api/pin/status")
configured=$(echo "$out" | jq -r .configured 2>/dev/null)
[ "$configured" = "true" ] && ok "PIN is configured" || bad "PIN status returned: $out"

# 5. account summary
step "5. /chat/api/account/summary"
out=$(curl -s -H "$AUTH" "$HOST/chat/api/account/summary")
nl=$(echo "$out" | jq -r .net_liquidation 2>/dev/null)
[ -n "$nl" ] && [ "$nl" != "null" ] && ok "net_liquidation=$nl" || bad "no net_liquidation: $out"

# 6. user_prefs CRUD round-trip
step "6. /chat/api/prefs CRUD round-trip"
testkey="verifyKey$(date +%s)"
curl -s -H "$AUTH" -H "Content-Type: application/json" -X POST \
  -d "{\"key\":\"$testkey\",\"value\":\"verifyVal\"}" "$HOST/chat/api/prefs" >/dev/null
got=$(curl -s -H "$AUTH" "$HOST/chat/api/prefs" | jq -r ".[\"$testkey\"]")
[ "$got" = "verifyVal" ] && ok "set+list round-trip ($testkey=verifyVal)" || bad "got '$got'"
curl -s -H "$AUTH" -X DELETE "$HOST/chat/api/prefs/$testkey" >/dev/null
got=$(curl -s -H "$AUTH" "$HOST/chat/api/prefs" | jq -r ".[\"$testkey\"] // \"GONE\"")
[ "$got" = "GONE" ] && ok "delete removes key" || bad "delete didn't take ($got)"

# 7. Watchlist CRUD round-trip
step "7. /chat/api/watchlists CRUD round-trip"
wlname="VerifyWL_$(date +%s)"
wlid=$(curl -s -H "$AUTH" -H "Content-Type: application/json" -X POST \
  -d "{\"name\":\"$wlname\"}" "$HOST/chat/api/watchlists" | jq -r .id)
[ -n "$wlid" ] && [ "$wlid" != "null" ] && ok "POST created id=$wlid" || bad "create failed"

# 7b. add a stock
curl -s -H "$AUTH" -H "Content-Type: application/json" -X POST \
  -d '{"symbol":"AAPL"}' "$HOST/chat/api/watchlists/$wlid/stocks" >/dev/null
syms=$(curl -s -H "$AUTH" "$HOST/chat/api/watchlists/$wlid/stocks" | jq -r '[.[].symbol] | @csv')
[ "$syms" = '"AAPL"' ] && ok "added AAPL, list shows: $syms" || bad "got: $syms"

# 7c. duplicate add → 409
code=$(curl -s -o /dev/null -w '%{http_code}' -H "$AUTH" -H "Content-Type: application/json" -X POST \
  -d '{"symbol":"AAPL"}' "$HOST/chat/api/watchlists/$wlid/stocks")
probe "POST duplicate AAPL → $code (expect 409)"
[ "$code" = "409" ] && ok "duplicate rejected" || bad "expected 409 got $code"

# 7d. PATCH metrics
curl -s -H "$AUTH" -H "Content-Type: application/json" -X PATCH \
  -d '{"rating":"BUY","current_price":189.50,"target_price":210.0}' \
  "$HOST/chat/api/watchlists/$wlid/stocks/AAPL" >/dev/null
got=$(curl -s -H "$AUTH" "$HOST/chat/api/watchlists/$wlid/stocks" | jq -r '.[0].rating')
[ "$got" = "BUY" ] && ok "PATCH rating saved" || bad "got '$got'"

# 7e. DELETE stock → list empty
curl -s -H "$AUTH" -X DELETE "$HOST/chat/api/watchlists/$wlid/stocks/AAPL" >/dev/null
n=$(curl -s -H "$AUTH" "$HOST/chat/api/watchlists/$wlid/stocks" | jq 'length')
[ "$n" = "0" ] && ok "DELETE removed AAPL" || bad "list still has $n entries"

# 7f. DELETE watchlist (cleanup)
curl -s -H "$AUTH" -X DELETE "$HOST/chat/api/watchlists/$wlid" >/dev/null
gone=$(curl -s -H "$AUTH" "$HOST/chat/api/watchlists" | jq -r ".[] | select(.id==$wlid) | .id")
[ -z "$gone" ] && ok "watchlist deleted cleanly" || bad "still listed (id=$gone)"

# 8. Threads CRUD (the persistence fix relies on this)
step "8. /chat/api/threads (chat memory persistence)"
tid=$(curl -s -H "$AUTH" -H "Content-Type: application/json" -X POST \
  -d '{"title":"verify-thread","client_id":"verify-cli"}' "$HOST/chat/api/threads" | jq -r .id)
[ -n "$tid" ] && [ "$tid" != "null" ] && ok "POST created tid=$tid" || bad "thread create failed"

# 8b. list threads → tid is present
listed=$(curl -s -H "$AUTH" "$HOST/chat/api/threads" | jq -r ".threads[] | select(.id==\"$tid\") | .id")
[ "$listed" = "$tid" ] && ok "list returns the new thread" || bad "thread not in list"

# 8c. fetch messages of new thread → empty
nmsgs=$(curl -s -H "$AUTH" "$HOST/chat/api/threads/$tid/messages" | jq '.messages | length')
[ "$nmsgs" = "0" ] && ok "fresh thread has 0 messages" || bad "expected 0, got $nmsgs"

# 8d. fetch nonexistent thread → 404
code=$(curl -s -o /dev/null -w '%{http_code}' -H "$AUTH" \
  "$HOST/chat/api/threads/thr_nope_nope_nope/messages")
probe "GET messages of nonexistent thread → $code (expect 404)"
[ "$code" = "404" ] && ok "404 for missing thread" || bad "expected 404 got $code"

# 8e. DELETE thread (cleanup)
curl -s -H "$AUTH" -X DELETE "$HOST/chat/api/threads/$tid" >/dev/null

# 9. PIN unlock works with right + wrong PIN
step "9. /chat/api/pin/unlock"
code=$(curl -s -o /dev/null -w '%{http_code}' -H "Content-Type: application/json" -X POST \
  -d "{\"pin\":\"$PIN\"}" "$HOST/chat/api/pin/unlock")
[ "$code" = "200" ] && ok "correct PIN → 200" || bad "correct PIN got $code"
code=$(curl -s -o /dev/null -w '%{http_code}' -H "Content-Type: application/json" -X POST \
  -d '{"pin":"0000"}' "$HOST/chat/api/pin/unlock")
probe "wrong PIN → $code (expect 401)"
[ "$code" = "401" ] && ok "wrong PIN rejected" || bad "expected 401 got $code"

# 10. tool list registered (smoke: at least one tool listed)
step "10. chat tool registration"
out=$(curl -s -H "$AUTH" "$HOST/chat/api/health")
tcount=$(echo "$out" | jq -r .tool_count)
[ "$tcount" -gt 5 ] && ok "$tcount tools available to chat agent" \
  || bad "expected >5 tools, got $tcount"

# Summary
echo
echo "═══════════════════════════════════════════"
echo "  PASS: $PASS    FAIL: $FAIL"
echo "═══════════════════════════════════════════"
rm -rf "$TMPDIR"
