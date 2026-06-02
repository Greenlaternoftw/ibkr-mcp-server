"""Phone-alert helper (ntfy.sh).

Posts a one-line push notification to an ntfy.sh topic. Designed so the
daemon can shout for help when IBKR drops without bringing operational
complexity into the hot path:

  * **Failure-silent.** The notify path NEVER raises into a caller. A
    transient ntfy outage or DNS hiccup cannot break the daemon's
    disconnect handler. We log+swallow.
  * **Off by default.** ``NOTIFY_ENABLED=false`` returns immediately,
    so unit tests and dev runs don't accidentally page the user.
  * **Stdlib-only.** No new runtime dep. urllib is enough for a single
    POST with a short timeout.
  * **Non-blocking.** ``send()`` returns immediately on the asyncio
    loop; the actual HTTP call runs in a default-executor thread.

Companion alerts when the daemon itself is hung (and therefore CAN'T
notify) come from ``scripts/ibkr-watchdog.sh``, which posts the same
ntfy topic from cron.

Setup (in .env):
    NOTIFY_ENABLED=true
    NTFY_TOPIC=ibkr-<random>     # pick something unguessable; topic is public
    NTFY_URL=https://ntfy.sh     # default — override only for self-hosted ntfy
"""

from __future__ import annotations

import asyncio
import logging
import urllib.error
import urllib.request
from typing import Iterable, Optional

from .config import settings

logger = logging.getLogger(__name__)

# Short timeout: a slow ntfy must NEVER stall the daemon. If the POST
# can't finish in 3s we drop it.
_HTTP_TIMEOUT = 3.0


def _build_request(
    *,
    title: str,
    message: str,
    priority: int,
    tags: Optional[Iterable[str]],
) -> Optional[urllib.request.Request]:
    """Build a ready-to-send ntfy POST. Returns None if config is invalid."""
    base = (settings.ntfy_url or "").rstrip("/")
    topic = settings.ntfy_topic or ""
    if not base or not topic:
        return None

    url = f"{base}/{topic}"
    headers = {
        "Title": title,
        "Priority": str(priority),
        "Content-Type": "text/plain; charset=utf-8",
    }
    if tags:
        headers["Tags"] = ",".join(tags)

    return urllib.request.Request(
        url,
        data=message.encode("utf-8"),
        headers=headers,
        method="POST",
    )


def _post_sync(req: urllib.request.Request) -> None:
    """Blocking POST, always swallows errors."""
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            # Read+discard so the connection closes cleanly.
            resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # ntfy outage / DNS / cert problem -- log at debug; do NOT raise.
        logger.debug("notify: ntfy POST failed (swallowed): %s", e)
    except Exception as e:  # pragma: no cover -- defence in depth
        logger.debug("notify: unexpected error in POST (swallowed): %s", e)


def send(
    title: str,
    message: str,
    *,
    priority: int = 3,
    tags: Optional[Iterable[str]] = None,
) -> None:
    """Fire-and-forget ntfy notification.

    Safe to call from sync or async context. If an asyncio loop is
    running, the HTTP POST is dispatched to the default executor so
    the loop never blocks. Otherwise the call is synchronous.

    Parameters
    ----------
    title : short headline shown as the push title.
    message : body of the alert.
    priority : ntfy 1 (min) -- 5 (urgent). Default 3.
    tags : optional iterable of emoji tags ("warning", "rotating_light", ...).
    """
    if not settings.notify_enabled:
        return

    req = _build_request(title=title, message=message, priority=priority, tags=tags)
    if req is None:
        logger.debug(
            "notify: NOTIFY_ENABLED=true but ntfy_url/ntfy_topic empty; skipping"
        )
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop running -- do the POST inline.
        _post_sync(req)
        return

    # Hand off to a thread so we don't block the asyncio loop.
    loop.run_in_executor(None, _post_sync, req)


# --- convenience wrappers for the two events the user asked for -----------
#
# Keep these as named functions (not just string literals at call sites) so
# wording stays consistent and future tweaks happen in one place.


def alert_disconnect(reason: str = "") -> None:
    """Daemon lost its connection to IBKR Gateway."""
    body = "Daemon lost its connection to IBKR Gateway."
    if reason:
        body += f" Reason: {reason}"
    body += " Watchdog will attempt auto-recovery within 5 minutes."
    send(
        title="IBKR disconnected",
        message=body,
        priority=4,
        tags=["warning", "electric_plug"],
    )


def alert_reconnect() -> None:
    """Daemon's IBKR connection was restored after a prior drop."""
    send(
        title="IBKR reconnected",
        message="Daemon connection to IBKR Gateway restored.",
        priority=2,
        tags=["white_check_mark"],
    )


def alert_reconnect_failed(attempts: int, duration_seconds: int) -> None:
    """Daemon's persistent reconnect loop gave up.

    Means we tried for the configured ceiling (default 10 minutes) and
    couldn't get IBKR back. At this point the watchdog cron is the
    next line of defence — it'll restart the daemon process from
    scratch, which often clears whatever was stuck.
    """
    send(
        title="IBKR reconnect failed",
        message=(
            f"Daemon retried {attempts} times over {duration_seconds}s without "
            "restoring IBKR. Watchdog will restart the daemon on its next tick. "
            "If this keeps recurring, check Gateway container logs."
        ),
        priority=5,
        tags=["rotating_light", "x"],
    )
