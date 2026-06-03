"""Rate-limiter + lockout state for the chat-UI PIN unlock endpoint.

A 4-digit numeric PIN has only 10,000 combinations, so the rate-limiter
is what actually keeps it secure (not the digit count). Without it, a
casual attacker on the Tailscale network could try every PIN in a few
seconds.

Two layers of throttling:

  * **Rate limit** -- max 5 failed attempts per 60s (sliding window).
    Returns HTTP 429 above the threshold. Prevents tight loops.
  * **Lockout** -- max 10 failed attempts per hour. After threshold,
    every subsequent attempt returns 429 for the rest of the hour
    even if the user spaces them out. Fires an ntfy push so the
    operator knows someone (maybe them, maybe not) is hammering it.

State is in-memory and per-process. Daemon restart resets the counter;
that's fine -- attacker has to start over too.

Bearer-token auth is unaffected by this lockout. If you forget your
PIN and trigger the lockout, you can still log in via Claude Desktop /
curl with the bearer token and either reset the lockout or change
``CHAT_PIN`` in ``.env``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Deque

from .. import notify

logger = logging.getLogger(__name__)


# Rate-limit knobs. Lowering threshold or shortening the window makes
# the unlock endpoint stricter; raising them is more permissive. Defaults
# tuned for a 4-digit numeric PIN at personal-trading scale.
RATE_THRESHOLD = 5          # max failures in...
RATE_WINDOW_SECONDS = 60    # ...this many seconds (sliding) before 429
LOCKOUT_THRESHOLD = 10      # max failures in...
LOCKOUT_WINDOW_SECONDS = 3600   # ...this many seconds (sliding) before 1-hour lockout

# Maximum entries we'll keep in the failure deque. Sized comfortably
# above LOCKOUT_THRESHOLD so old entries don't crowd out new ones, but
# bounded so a sustained attack can't leak memory.
_DEQUE_MAX = 200

_failures: Deque[float] = deque(maxlen=_DEQUE_MAX)
_lock = threading.Lock()

# Track whether we've already alerted for the current lockout window
# so a sustained attack doesn't spam your phone with one push per
# failed attempt.
_alerted_at: float = 0.0


def _prune(now: float) -> None:
    """Drop failures older than the longest window we care about."""
    cutoff = now - LOCKOUT_WINDOW_SECONDS
    while _failures and _failures[0] < cutoff:
        _failures.popleft()


def status() -> str:
    """Return current throttle state for the unlock endpoint.

    One of: ``"ok"`` / ``"rate_limited"`` / ``"locked_out"``.

    ``"rate_limited"`` -- recent burst of failures; client should back
    off for a minute.
    ``"locked_out"`` -- sustained failures; client is blocked for the
    rest of the hour even if it stops trying.
    """
    now = time.time()
    with _lock:
        _prune(now)
        if len(_failures) >= LOCKOUT_THRESHOLD:
            return "locked_out"
        recent = sum(1 for t in _failures if t > now - RATE_WINDOW_SECONDS)
        if recent >= RATE_THRESHOLD:
            return "rate_limited"
        return "ok"


def record_failure() -> str:
    """Record one failed PIN attempt and return the new status.

    Caller should check the returned status; if it just transitioned
    to ``"locked_out"``, the route handler fires the ntfy alert via
    :func:`maybe_alert_lockout`.
    """
    now = time.time()
    with _lock:
        _failures.append(now)
        _prune(now)
        if len(_failures) >= LOCKOUT_THRESHOLD:
            return "locked_out"
        recent = sum(1 for t in _failures if t > now - RATE_WINDOW_SECONDS)
        if recent >= RATE_THRESHOLD:
            return "rate_limited"
        return "ok"


def record_success() -> None:
    """Clear all recorded failures on a successful unlock.

    Stops a near-miss attacker who guessed the PIN on attempt 9 from
    leaving 9 entries in the deque -- the next user (you) shouldn't
    inherit that history.
    """
    with _lock:
        _failures.clear()
    global _alerted_at
    _alerted_at = 0.0


def maybe_alert_lockout() -> None:
    """Fire an ntfy push on transition to lockout. De-duped per window.

    Called by the route handler immediately after ``record_failure()``
    returns ``"locked_out"``. We check the dedup ourselves so a sustained
    attack (each failure also returning ``"locked_out"`` because the
    window is still full) doesn't generate a push for every attempt.
    """
    global _alerted_at
    now = time.time()
    # If we alerted within the current lockout window, stay quiet.
    if now - _alerted_at < LOCKOUT_WINDOW_SECONDS:
        return
    _alerted_at = now
    logger.warning("chat PIN: lockout triggered -- sending ntfy alert")
    notify.send(
        title="Chat PIN locked out",
        message=(
            f"{LOCKOUT_THRESHOLD}+ failed PIN attempts in the last hour. "
            "PIN unlock blocked for 1 hour. Bearer token still works "
            "(Claude Desktop, curl, etc.) -- log in normally and either "
            "change CHAT_PIN in .env or just wait it out."
        ),
        priority=5,
        tags=["rotating_light", "lock"],
    )


def reset() -> None:
    """Test hook -- wipes all in-memory state."""
    global _alerted_at
    with _lock:
        _failures.clear()
    _alerted_at = 0.0
