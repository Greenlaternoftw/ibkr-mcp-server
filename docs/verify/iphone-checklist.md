# iPhone Safari verification checklist

For each step: drive the app, observe, mark ✅ / ❌ / ⚠️.
URL: http://100.76.239.39:8765/chat (Tailscale required on the iPhone).

## A. Boot + PIN gate

1. **Open Safari, load the URL.** Login overlay should appear instantly.
   Title "Welcome Back · Enter your PIN to unlock", 4 empty dots, full keypad
   (1-9, 0, ⌫). No system `prompt()` dialog. → ✅ / ❌

2. **Wrong PIN once** (any 4 digits that aren't the real PIN). Dots shake red,
   "Incorrect PIN · 1 attempt" appears below. → ✅ / ❌

3. **Correct PIN.** Keypad fades, Command Center loads behind it. → ✅ / ❌

## B. Dashboard sanity

4. **Account-summary strip** (6 tiles under topbar): NET LIQ, BUY PWR,
   EXCESS LIQ, MAINT MARGIN, REALIZED, UNREALIZED. Within ~1s after unlock
   all 6 should show numbers, **not `—`**. → ✅ / ❌

5. **Topbar layout**: orange topbar at top, sub-line "v1 · analyst-gated
   execution", ⚙ button visible on the right. → ✅ / ❌

## C. Watchlist tabs + the new add-tab modal

6. **Existing tabs** ("Watchlist", "Portfolio") render along the top of the
   watchlist section. → ✅ / ❌

7. **Tap the "+" button** next to the tabs. An **inline modal** opens titled
   "Add a watchlist or portfolio" with a text input. **NOT** a native iOS
   prompt dialog. → ✅ / ❌

8. Type `Speculation` → tap **Add**. New tab appears, becomes active. → ✅ / ❌

9. **Refresh the page** (close Safari tab, reopen, unlock). The
   `Speculation` tab is still there. → ✅ / ❌  (server-persisted, not localStorage)

## D. Watchlist row workflow

10. With `Speculation` active, in the symbol input type `AAPL` → tap
    Research (or Enter). Row appears in the table within ~3s with rating +
    price. → ✅ / ❌

11. **Remove the row** with the ✕ button. Confirm prompt → OK → row vanishes
    immediately. → ✅ / ❌

## E. Settings panel (cleanup verification)

12. Tap ⚙. Settings modal opens. → ✅ / ❌

13. The modal contains:
    - Intro line "Daemon-routed · IBKR MCP attached server-side ..." or
      similar
    - Status rows: **Daemon ✓ ready**, **Model claude-...**, **Tools loaded** (a number), **PWA install**
    - "Test Daemon" button + result line
    - 🔒 **Lock App Now** button
    - Single **Close** button at the bottom

    **NOT** present (regression check):
    - ❌ MCP Server URL input field
    - ❌ MCP Name input field
    - ❌ "Save" button
    → ✅ / ❌

14. Tap **Test Daemon**. Result line shows green "✓ Daemon ready. model=...,
    tools=N". → ✅ / ❌

## F. View toggle + last-view restoration (NEW)

15. Top-right "Chat View" button. Tap it. Page flips to chat pane. → ✅ / ❌

16. Type any message in chat (e.g. "hello"), send. You see streaming reply
    from Claude. → ✅ / ❌

17. Toggle back to Dashboard via the same button. Do anything (add a watchlist
    symbol, refresh a price). → ✅ / ❌

18. Toggle back to Chat. Your earlier "hello" + reply is **still there**
    (history preserved across view toggles in the same session). → ✅ / ❌

19. **The big one — chat persistence across page reload.**
    Close the Safari tab entirely (swipe up, swipe away). Reopen Safari, load
    the URL, unlock with PIN.

    Expected:
    - Page lands on **Chat view** (because that's where you were when you
      closed), not the dashboard. → ✅ / ❌
    - Your earlier "hello" + Claude's reply is **still in the chat panel**,
      not wiped. → ✅ / ❌

20. Tap **Clear chat**. Confirms → wipes the visible history. Send a new
    message; the new message starts a fresh transcript (no old "hello"). → ✅ / ❌

## G. Chat agent fixes (be08732 + 08ba98c)

21. From a fresh chat (after step 20 clear), send:
    `What's my AAPL position?` or any message that triggers a tool call.

    Expected:
    - Claude responds, you see a 🔧 tool-call chip
    - Then a ✓ tool-result chip
    - Then Claude's text reply
    → ✅ / ❌

22. **Then send a follow-up**: `What's my cash balance?`

    Expected:
    - No 400 error. (This is the role:tool fix.)
    - Claude responds with another tool call + answer.
    → ✅ / ❌  (this was broken before be08732)

23. **TRAIL BUY test.** Send:
    `Place a TRAILING BUY for AAPL via the IBKR MCP. action=BUY,
    quantity=10, order_type=TRAIL, trail_amount=2.00, tif=GTC,
    dry_run=true. Confirm the order type is supported.`

    Expected:
    - Claude does NOT say "trailing buy isn't supported"
    - Claude calls `place_order` with the right params
    - The dry_run preview comes back
    → ✅ / ❌  (this was broken before 08ba98c)

## H. Quick Exit / Quick Buy dispatch (paper account only!)

24. In Dashboard view, find a watchlist row with a price. Tap the **EXIT**
    button (only shows on portfolio-type tabs with a position). Modal opens
    with quantity preset (Half / Quarter / All) + dollar/share input. → ✅ / ❌

25. Enter `half`, tap Execute. Modal closes, view flips to Chat, the FAST
    EXIT prompt streams in and Claude calls `place_order`. The destructive-
    action confirmation gate should pause. **Do not confirm.** → ✅ / ❌

26. Similar for **BUY** button → Quick Buy modal → enter `$1000`, tap Execute.
    Modal closes, view flips to Chat, BUY command streams. → ✅ / ❌

## I. PWA + iOS notch (added in G phase)

27. **Add to Home Screen**: Safari share → Add to Home Screen → name it
    "IBKR" → Add. → ✅ / ❌

28. **Launch from the new home-screen icon.** Should open full-screen (no
    Safari URL bar at the bottom, no tab bar at the top). Orange topbar sits
    **below** the notch, not under it. → ✅ / ❌

## J. Lock + relock

29. ⚙ → 🔒 Lock App Now. Keypad reappears. → ✅ / ❌
30. Re-enter PIN. Land back on the last view (the view you were on before
    locking). → ✅ / ❌
