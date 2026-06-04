"""Live-trading safety rails.

When the daemon is connected to a LIVE IBKR account (ibkr_is_paper=False)
this module enforces additional safety beyond the existing paper-mode
guards:

  - Daily realized-P&L floor: when today's cumulative realized P&L
    crosses below `live_daily_loss_limit` (e.g. -$500), the
    circuit-breaker trips and auto-pauses all autonomous pivot loops.
    Operator must manually resume.

  - max_order_size override: in live mode, the effective cap is
    `live_max_order_size` (default 100) rather than `max_order_size`
    (default 1000 for paper). Forces conservative sizing.

  - Loops-auto-pause-on-first-connect: any pivot loops carried over
    from paper mode get auto-paused on the first live connect, so
    nothing trades automatically without explicit operator approval.

The circuit-breaker state is kept in-process. Restarting the daemon
re-evaluates from the current day's realized P&L (read from
ib.portfolio() summary values) so a restart doesn't reset a tripped
breaker artificially.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Optional

from .config import settings

logger = logging.getLogger(__name__)


@dataclass
class CircuitBreakerState:
    """In-process state of the daily loss circuit breaker."""
    tripped: bool = False
    tripped_at: Optional[dt.datetime] = None
    tripped_at_pnl: Optional[float] = None
    reset_for_date: Optional[dt.date] = None


_breaker = CircuitBreakerState()


def is_live_mode() -> bool:
    """Resolve live-vs-paper from config. Single source of truth so the
    engine + order path + dashboard all agree on the answer."""
    return not bool(settings.ibkr_is_paper)


def effective_max_order_size() -> int:
    """The cap that should actually be enforced right now, accounting
    for live-mode override."""
    if is_live_mode():
        return min(settings.live_max_order_size, settings.max_order_size)
    return settings.max_order_size


def get_breaker_state() -> CircuitBreakerState:
    """Read-only access to the breaker for status displays / tests."""
    return _breaker


def _maybe_reset_for_new_day(today: dt.date) -> None:
    """Daily P&L resets at midnight ET. If we're on a new day and the
    breaker was tripped yesterday, allow it to be evaluated fresh."""
    if _breaker.reset_for_date != today:
        _breaker.reset_for_date = today
        if _breaker.tripped:
            logger.info(
                f"live circuit breaker: daily reset to fresh state "
                f"(was tripped on {_breaker.tripped_at} at P&L "
                f"${_breaker.tripped_at_pnl})"
            )
        _breaker.tripped = False
        _breaker.tripped_at = None
        _breaker.tripped_at_pnl = None


def check_daily_pnl_breaker(
    current_realized_pnl: float,
    *,
    today: Optional[dt.date] = None,
) -> bool:
    """Evaluate the breaker against the latest realized P&L.

    Returns True if the breaker is currently TRIPPED (callers should
    refuse new entries / auto-pause autonomous loops).

    In paper mode, always returns False (the rail is live-mode only).

    Idempotent: re-calling after a trip just confirms the tripped
    state until the date rolls over.
    """
    if not is_live_mode():
        return False
    today = today or dt.date.today()
    _maybe_reset_for_new_day(today)
    if _breaker.tripped:
        return True
    if current_realized_pnl <= settings.live_daily_loss_limit:
        _breaker.tripped = True
        _breaker.tripped_at = dt.datetime.now(dt.timezone.utc)
        _breaker.tripped_at_pnl = current_realized_pnl
        logger.warning(
            f"live circuit breaker: TRIPPED at realized P&L "
            f"${current_realized_pnl:.2f} (limit "
            f"${settings.live_daily_loss_limit:.2f})"
        )
        # Best-effort ntfy push -- swallow any failure so the breaker
        # check itself never raises.
        try:
            from .notify import send
            send(
                title="🛑 Live circuit breaker TRIPPED",
                message=(
                    f"Daily realized P&L ${current_realized_pnl:.2f} "
                    f"≤ limit ${settings.live_daily_loss_limit:.2f}. "
                    f"All autonomous loops auto-paused. "
                    f"Manual resume required."
                ),
                priority=5,
                tags=["rotating_light", "money_with_wings"],
            )
        except Exception:
            pass
        return True
    return False


def manual_reset_breaker() -> None:
    """Operator escape hatch. Use sparingly -- the whole point of the
    breaker is to force a pause-and-think after a bad day. Logged
    and ntfy'd so the action is audit-trailed."""
    if not _breaker.tripped:
        return
    logger.warning(
        f"live circuit breaker: MANUAL RESET (was tripped at "
        f"${_breaker.tripped_at_pnl})"
    )
    try:
        from .notify import send
        send(
            title="⚠️ Live circuit breaker manually reset",
            message=(
                "Operator cleared the daily loss circuit breaker. "
                "Autonomous loops can resume if manually unpaused."
            ),
            priority=4,
            tags=["warning"],
        )
    except Exception:
        pass
    _breaker.tripped = False
    _breaker.tripped_at = None
    _breaker.tripped_at_pnl = None


def status_dict() -> dict:
    """JSON-shaped status for the dashboard banner + status endpoint."""
    return {
        "live_mode": is_live_mode(),
        "max_order_size_effective": effective_max_order_size(),
        "daily_loss_limit": settings.live_daily_loss_limit if is_live_mode() else None,
        "breaker_tripped": _breaker.tripped if is_live_mode() else False,
        "breaker_tripped_at": _breaker.tripped_at.isoformat() if _breaker.tripped_at else None,
        "breaker_tripped_at_pnl": _breaker.tripped_at_pnl,
        "live_auto_pause_loops_on_connect": settings.live_auto_pause_loops_on_connect if is_live_mode() else None,
        "live_ntfy_every_order": settings.live_ntfy_every_order if is_live_mode() else None,
    }
