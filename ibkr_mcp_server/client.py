"""IBKR Client with advanced trading capabilities."""

import asyncio
import datetime as dt
import logging
import time
from dataclasses import asdict
from typing import Any, Dict, List, Optional, Union
from decimal import Decimal

from ib_async import IB, MarketOrder, Stock, util
from .config import settings
from .orders import (
    OrderRequest,
    build_order,
    make_preview,
    validate_request,
)
from .oca import prepare_group
from .regime import RegimeConfig, check_regime_from_bars
from .reversal import (
    DEFAULT_STATE_PATH as REVERSAL_STATE_PATH,
    FilledTranche,
    ReversalConfig,
    ReversalState,
    ReversalStatus,
    check_reversal_signals_from_bars,
    decide_next_action,
    load_state as load_reversal_state,
    save_state as save_reversal_state,
)
from .swing import (
    DEFAULT_STATE_PATH as SWING_STATE_PATH,
    SwingConfig,
    SwingState,
    SwingStateRecord,
    apply_fill,
    decide_next_action as swing_decide_next_action,
    detect_fills_from_trades,
    load_state as load_swing_state,
    save_state as save_swing_state,
)
from .oca import make_group_id
from .utils import rate_limit, retry_on_failure, safe_float, safe_int, ValidationError, ConnectionError as IBKRConnectionError
from . import notify


def _classify_shortable(raw: Optional[float]) -> str:
    """Map IB's log-encoded shortableShares to a human-readable category.

    IBKR reports shortable availability on a log-ish scale where values >= 3.0
    mean "plenty available" (easy to borrow), 2.5–3.0 means "hard but possible",
    and below 2.5 / negative typically means unavailable. Values are an
    approximation of orders of magnitude (so 3.0 ~= 1000 shares, 4.0 ~=
    10000), but the exact thresholds vary by broker tier — verify against
    your account if precision matters.
    """
    if raw is None:
        return "unknown"
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return "unknown"
    if v >= 3.0:
        return "easy_to_borrow"
    if v >= 2.5:
        return "hard_to_borrow"
    return "not_available"


class IBKRClient:
    """Enhanced IBKR client with multi-account and short selling support."""
    
    def __init__(self):
        self.ib: Optional[IB] = None
        self.logger = logging.getLogger(__name__)
        
        # Connection settings
        self.host = settings.ibkr_host
        self.port = settings.ibkr_port
        self.client_id = settings.ibkr_client_id
        self.max_reconnect_attempts = settings.max_reconnect_attempts
        self.reconnect_delay = settings.reconnect_delay
        self.reconnect_attempts = 0
        
        # Account management
        self.accounts: List[str] = []
        self.current_account: Optional[str] = settings.ibkr_default_account
        
        # Connection state
        self._connected = False
        self._connecting = False
        # _had_prior_connection: distinguishes "first connect since process
        # start" from "reconnect after a drop". Only the latter fires a
        # reconnect alert — we don't want a notification every time the
        # daemon boots cleanly. Flipped True by _on_disconnect.
        self._had_prior_connection = False
        # Guard against double-firing the disconnect alert during ib_async's
        # built-in retry storms (it can re-emit disconnectedEvent multiple
        # times for a single physical drop). Reset by a successful connect.
        self._disconnect_alert_sent = False

        # Layer 3 — reversal entry: per-symbol state + background tasks.
        # Loaded lazily on first access (so tests with isolated state paths
        # don't read a real user's file). Layer 5 daemon will replace this.
        self._reversal_states: Dict[str, ReversalState] | None = None
        self._reversal_tasks: Dict[str, asyncio.Task] = {}
        self._reversal_state_path = REVERSAL_STATE_PATH

        # Layer 4 — swing-trading loop.
        self._swing_states: Dict[str, SwingStateRecord] | None = None
        self._swing_tasks: Dict[str, asyncio.Task] = {}
        self._swing_state_path = SWING_STATE_PATH

        # Layer 5 — pivot-loop engine (Phase B). One asyncio Task per
        # active loop; state lives in chat.db (pivot_loops table). See
        # ibkr_mcp_server.pivot_loop for the decision policy + tick logic.
        self._pivot_tasks: Dict[str, asyncio.Task] = {}

        # Bug class #5/#6/#7 — single bad IB call could wedge the entire server
        # via unbounded awaits on a shared connection. Three defences:
        #   1. _order_lock serializes order placements so a slow one can't
        #      interleave with another order. Reads stay parallel.
        #   2. _bounded() wraps every IB API call in asyncio.wait_for so no
        #      single call can hang the handler longer than its timeout.
        #   3. _reset_on_timeout() force-disconnects+reconnects when an IB
        #      call times out, clearing any stuck waits in the underlying
        #      ib_async event loop. This is what prevents the *whole-server*
        #      wedge — without it, even after a timeout returns, the next
        #      call inherits the same broken state.
        self._order_lock = asyncio.Lock()
        self._resetting = False
    
    @property
    def is_paper(self) -> bool:
        """Check if this is a paper trading connection."""
        return self.port in [7497, 4002]  # Common paper trading ports
    
    async def _ensure_connected(self) -> bool:
        """Ensure IBKR connection is active, reconnect if needed."""
        if self.is_connected():
            return True

        try:
            await self.connect()
            return self.is_connected()
        except Exception as e:
            self.logger.error(f"Failed to ensure connection: {e}")
            return False

    # --- destructive-tool confirmation gate -------------------------------
    #
    # When settings.require_confirmation_for_destructive_tools is True, tools
    # that cancel orders, stop strategies, or transmit live orders return a
    # "needs_confirmation" preview unless called with confirm=True. Off by
    # default. Designed to prevent chat sessions from issuing destructive
    # actions without an explicit second step (e.g., "stop my F swing"
    # cancelling a protective trail+stop pair in one shot, as happened
    # 2026-05-17).

    def _needs_confirmation(self, confirm: bool) -> bool:
        """True if the gate is enabled and confirm wasn't passed."""
        return bool(settings.require_confirmation_for_destructive_tools) and not confirm

    @staticmethod
    def _confirm_response(action: str, preview: Dict, hint: str) -> Dict:
        """Standard response shape when a destructive call needs confirm."""
        return {
            "status": "needs_confirmation",
            "action": action,
            "preview": preview,
            "message": hint,
        }

    # --- IB call safety helpers (Bug #5/#6/#7 defences) -------------------

    # Default per-call timeouts. Tune per operation if needed.
    DEFAULT_IB_TIMEOUT = 10.0
    QUALIFY_TIMEOUT = 5.0
    ORDER_ACK_TIMEOUT = 5.0
    HISTDATA_TIMEOUT = 20.0
    SUMMARY_TIMEOUT = 5.0

    # _reconnect() loop tuning. The default retry_on_failure decorator on
    # connect() gives up after 3 attempts in ~8 seconds — far too short for
    # an IBKR-Gateway restart, where IBC needs 60-90 seconds to re-login.
    # The persistent loop below keeps polling until connect() succeeds or
    # RECONNECT_MAX_DURATION elapses.
    RECONNECT_RETRY_INTERVAL = 5.0   # seconds between attempts
    RECONNECT_MAX_DURATION = 600.0   # 10 min ceiling, then alert + give up

    async def _bounded(self, coro, *, timeout: float, op: str):
        """Run an IB coroutine with a hard timeout.

        On timeout, schedules a connection reset and re-raises TimeoutError.
        The reset is what prevents the whole-server wedge bug class: without
        it, the next IB call inherits the same stuck event-loop state.
        """
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            self.logger.error(
                f"IB call timed out: op={op} timeout={timeout}s -- resetting connection"
            )
            # Fire-and-forget reset so callers see the timeout immediately and
            # the next caller starts on a fresh connection.
            asyncio.create_task(self._reset_on_timeout())
            raise

    async def _reset_on_timeout(self) -> None:
        """Force-disconnect and reconnect to clear stuck ib_async waits."""
        if self._resetting:
            return
        self._resetting = True
        try:
            try:
                if self.ib and self.ib.isConnected():
                    self.ib.disconnect()
            except Exception:
                pass
            self._connected = False
            await asyncio.sleep(0.5)
            try:
                await self.connect()
                self.logger.info("IB connection reset complete after timeout")
            except Exception as e:
                self.logger.error(f"IB reset reconnect failed: {e}")
        finally:
            self._resetting = False
    
    @retry_on_failure(max_attempts=3)
    async def connect(self) -> bool:
        """Establish connection and discover accounts."""
        if self._connected and self.ib and self.ib.isConnected():
            return True
        
        if self._connecting:
            # Wait for ongoing connection attempt
            while self._connecting:
                await asyncio.sleep(0.1)
            return self._connected
        
        self._connecting = True
        
        try:
            self.ib = IB()
            
            self.logger.info(f"Connecting to IBKR at {self.host}:{self.port}...")
            await self.ib.connectAsync(
                host=self.host,
                port=self.port,
                clientId=self.client_id,
                timeout=10
            )
            
            # Setup event handlers
            self.ib.disconnectedEvent += self._on_disconnect
            self.ib.errorEvent += self._on_error
            # Layer 5a: real-time fill detection for active strategies.
            self.ib.execDetailsEvent += self._on_exec_details_for_strategies
            self.ib.orderStatusEvent += self._on_order_status_for_strategies
            # Layer 5b: push every fill into the operator's active chat
            # thread as a synthetic message so the chat panel reflects
            # real-time order outcomes without the operator polling.
            self.ib.execDetailsEvent += self._on_fill_for_chat
            
            # Wait for connection to stabilize
            await asyncio.sleep(2)
            
            # Discover accounts
            self.accounts = self.ib.managedAccounts()
            if self.accounts:
                if not self.current_account or self.current_account not in self.accounts:
                    self.current_account = self.accounts[0]
                
                self.logger.info(f"Connected to IBKR. Accounts: {self.accounts}")
                self.logger.info(f"Current account: {self.current_account}")
            else:
                self.logger.warning("No managed accounts found")
            
            self._connected = True
            self.reconnect_attempts = 0

            # Phone alert: fire ONLY if this connect follows a prior drop,
            # not on the daemon's initial boot. The _on_disconnect handler
            # sets _had_prior_connection=True; we reset _disconnect_alert_sent
            # here so the next disconnect can alert again.
            if self._had_prior_connection and self._disconnect_alert_sent:
                notify.alert_reconnect()
            self._disconnect_alert_sent = False
            return True

        except Exception as e:
            self.logger.error(f"Failed to connect to IBKR: {e}")
            raise IBKRConnectionError(f"Connection failed: {e}")
        finally:
            self._connecting = False
    
    async def disconnect(self):
        """Clean disconnection."""
        if self.ib and self.ib.isConnected():
            self.ib.disconnect()
            self._connected = False
            self.logger.info("IBKR disconnected")
    
    def _on_disconnect(self):
        """Handle disconnection with automatic reconnection."""
        self._connected = False
        self.logger.warning("IBKR disconnected, scheduling reconnection...")
        # Phone alert: fire at most once per disconnect episode. ib_async
        # can re-emit disconnectedEvent during its own retry loop, so the
        # _disconnect_alert_sent flag de-dupes until a fresh connect() resets
        # it. _had_prior_connection arms the next connect() to fire a
        # "reconnected" alert.
        if not self._disconnect_alert_sent:
            self._disconnect_alert_sent = True
            self._had_prior_connection = True
            notify.alert_disconnect()
        asyncio.create_task(self._reconnect())
    
    def _on_error(self, reqId, errorCode, errorString, contract):
        """Centralized error logging."""
        # Don't log certain routine messages as errors
        if errorCode in [2104, 2106, 2158]:  # Market data warnings
            self.logger.debug(f"IBKR Info {errorCode}: {errorString}")
        else:
            self.logger.error(f"IBKR Error {errorCode}: {errorString} (reqId: {reqId})")
    
    async def _reconnect(self):
        """Persistent background reconnect after a drop.

        Why this is a loop and not just one connect() call:

        connect() is decorated with @retry_on_failure(max_attempts=3),
        which does ~8s of fast retries and then raises. That's fine for
        transient TCP blips but useless for the common IBKR-Gateway
        scenario where IBC needs 60-90s to log back in after a container
        restart or nightly server reset. The old single-shot _reconnect
        would give up at T+8s and leave the daemon stuck disconnected
        until the watchdog cron noticed 5 minutes later. That's a
        5-10 minute blind window in the middle of trading hours.

        New behavior: poll every RECONNECT_RETRY_INTERVAL seconds until
        either connect() succeeds (the success path in connect() fires
        the "reconnected" phone alert) or RECONNECT_MAX_DURATION elapses
        (then we alert + give up; the watchdog will daemon-restart us
        from a clean state).
        """
        start = time.monotonic()
        attempt = 0

        # Brief initial pause so we don't slam the broker at T+0; gives
        # Gateway a moment to start its own recovery.
        await asyncio.sleep(self.reconnect_delay)

        while time.monotonic() - start < self.RECONNECT_MAX_DURATION:
            attempt += 1
            try:
                if await self.connect():
                    # connect() success path fires alert_reconnect() and
                    # clears _disconnect_alert_sent. Nothing more to do.
                    self.logger.info(
                        f"Reconnect succeeded on attempt {attempt} "
                        f"after {int(time.monotonic() - start)}s"
                    )
                    return
            except Exception as e:
                # connect()'s @retry_on_failure already burned ~3-8s of
                # internal retries before raising, so we just log and
                # wait for the next outer-loop tick.
                self.logger.warning(
                    f"Reconnect attempt {attempt} failed: {e}. "
                    f"Will retry in {self.RECONNECT_RETRY_INTERVAL:.0f}s."
                )

            await asyncio.sleep(self.RECONNECT_RETRY_INTERVAL)

        elapsed = int(time.monotonic() - start)
        self.logger.error(
            f"Reconnect gave up after {attempt} attempts over {elapsed}s -- "
            "watchdog should daemon-restart us on its next cron tick"
        )
        notify.alert_reconnect_failed(attempts=attempt, duration_seconds=elapsed)
    
    def is_connected(self) -> bool:
        """Check connection status."""
        return self._connected and self.ib is not None and self.ib.isConnected()
    
    @rate_limit(calls_per_second=1.0)
    async def get_portfolio(self, account: Optional[str] = None) -> List[Dict]:
        """Get portfolio positions. Bounded by SUMMARY_TIMEOUT."""
        try:
            if not await self._ensure_connected():
                raise ConnectionError("Not connected to IBKR")

            account = account or self.current_account

            positions = await self._bounded(
                self.ib.reqPositionsAsync(),
                timeout=self.SUMMARY_TIMEOUT,
                op="reqPositions",
            )

            portfolio = []
            for pos in positions:
                if not account or pos.account == account:
                    portfolio.append(self._serialize_position(pos))

            return portfolio

        except asyncio.TimeoutError:
            self.logger.error("Portfolio request timed out")
            raise RuntimeError("IB call timed out fetching portfolio; connection reset")
        except Exception as e:
            self.logger.error(f"Portfolio request failed: {e}")
            raise RuntimeError(f"IBKR API error: {str(e)}")

    async def get_open_orders(self, account: Optional[str] = None) -> List[Dict]:
        """Return current open orders for the (active) account.

        Uses ``ib.openTrades()`` which is the cached, account-update-driven
        list of active trades (orders that have been transmitted and are
        either pending fill, partially filled, or awaiting cancellation).
        """
        if not await self._ensure_connected():
            raise IBKRConnectionError("Not connected to IBKR")
        target = account or self.current_account
        trades = list(self.ib.openTrades())
        out = []
        for t in trades:
            try:
                if target and getattr(t.order, "account", None) != target:
                    continue
                out.append({
                    "order_id": t.order.orderId,
                    "perm_id": getattr(t.order, "permId", None),
                    "symbol": t.contract.symbol,
                    "sec_type": getattr(t.contract, "secType", "STK"),
                    "action": t.order.action,
                    "order_type": t.order.orderType,
                    "quantity": float(t.order.totalQuantity or 0),
                    "limit_price": float(t.order.lmtPrice) if t.order.lmtPrice else None,
                    "stop_price": float(t.order.auxPrice) if t.order.auxPrice else None,
                    "tif": t.order.tif,
                    "status": t.orderStatus.status,
                    "filled": float(t.orderStatus.filled or 0),
                    "remaining": float(t.orderStatus.remaining or 0),
                    "parent_id": getattr(t.order, "parentId", 0) or None,
                    "oca_group": getattr(t.order, "ocaGroup", "") or None,
                })
            except Exception as e:
                self.logger.warning(f"open_orders: skipping malformed trade: {e}")
        return out

    async def cancel_order(self, order_id: int, confirm: bool = False) -> Dict:
        """Cancel one open order by IB order_id.

        Honors the destructive-action confirmation gate: when
        REQUIRE_CONFIRMATION_FOR_DESTRUCTIVE_TOOLS is set and ``confirm``
        is False, returns a preview without cancelling.
        """
        if not await self._ensure_connected():
            raise IBKRConnectionError("Not connected to IBKR")
        try:
            order_id = int(order_id)
        except (TypeError, ValueError):
            return {"status": "error", "message": f"invalid order_id: {order_id!r}"}

        target = None
        for t in self.ib.openTrades():
            if t.order.orderId == order_id:
                target = t
                break
        if target is None:
            return {
                "status": "not_found",
                "order_id": order_id,
                "message": f"no open order with id {order_id}",
            }

        if self._needs_confirmation(confirm):
            return self._confirm_response(
                action="cancel_order",
                preview={
                    "order_id": order_id,
                    "symbol": target.contract.symbol,
                    "action": target.order.action,
                    "order_type": target.order.orderType,
                    "quantity": float(target.order.totalQuantity or 0),
                    "filled": float(target.orderStatus.filled or 0),
                    "remaining": float(target.orderStatus.remaining or 0),
                    "status": target.orderStatus.status,
                },
                hint="Pass confirm=true to cancel this order.",
            )

        try:
            self.ib.cancelOrder(target.order)
        except Exception as e:
            return {"status": "error", "order_id": order_id, "message": str(e)}
        return {
            "status": "cancel_requested",
            "order_id": order_id,
            "symbol": target.contract.symbol,
            "action": target.order.action,
            "order_type": target.order.orderType,
        }

    async def cancel_all_orders(
        self,
        symbol: Optional[str] = None,
        account: Optional[str] = None,
        confirm: bool = False,
    ) -> Dict:
        """Cancel every open order for the active account, optionally
        filtered by symbol.

        Honors the destructive-action confirmation gate: returns a
        preview with the list of targeted orders unless ``confirm=True``.
        Uses per-order ``cancelOrder`` (not ``reqGlobalCancel``) so the
        filter applies cleanly and we can report per-order results.
        """
        if not await self._ensure_connected():
            raise IBKRConnectionError("Not connected to IBKR")
        target_acct = account or self.current_account
        sym_filter = symbol.upper() if isinstance(symbol, str) and symbol.strip() else None

        targets = []
        for t in self.ib.openTrades():
            if target_acct and getattr(t.order, "account", None) != target_acct:
                continue
            if sym_filter and t.contract.symbol.upper() != sym_filter:
                continue
            targets.append(t)

        if not targets:
            return {
                "status": "no_op",
                "cancelled": 0,
                "filter_symbol": sym_filter or "(all)",
                "message": "no matching open orders",
            }

        if self._needs_confirmation(confirm):
            preview_orders = [
                {
                    "order_id": t.order.orderId,
                    "symbol": t.contract.symbol,
                    "action": t.order.action,
                    "order_type": t.order.orderType,
                    "quantity": float(t.order.totalQuantity or 0),
                    "remaining": float(t.orderStatus.remaining or 0),
                    "status": t.orderStatus.status,
                }
                for t in targets[:30]   # cap preview to avoid wall-of-text
            ]
            return self._confirm_response(
                action="cancel_all_orders",
                preview={
                    "count": len(targets),
                    "filter_symbol": sym_filter or "(all symbols)",
                    "orders": preview_orders,
                    "truncated": len(targets) > 30,
                },
                hint=f"Pass confirm=true to cancel {len(targets)} order(s).",
            )

        cancelled = []
        errors = []
        for t in targets:
            try:
                self.ib.cancelOrder(t.order)
                cancelled.append({
                    "order_id": t.order.orderId,
                    "symbol": t.contract.symbol,
                    "action": t.order.action,
                    "order_type": t.order.orderType,
                })
            except Exception as e:
                errors.append({
                    "order_id": t.order.orderId,
                    "symbol": t.contract.symbol,
                    "error": str(e),
                })
        return {
            "status": "cancelled" if cancelled else "errored",
            "count": len(cancelled),
            "error_count": len(errors),
            "filter_symbol": sym_filter or "(all)",
            "cancelled": cancelled,
            "errors": errors,
        }

    # Bug #1 fix: ib_async's `reqAccountSummaryAsync()` no longer takes
    # positional `(group, tags)` args. The correct pattern is to start a
    # subscription with `reqAccountSummary()` (sync, no args) and read cached
    # values via `accountValues()`. We whitelist the important tags to avoid
    # returning ~80 fields of noise.
    IMPORTANT_ACCOUNT_TAGS = {
        "NetLiquidation", "TotalCashValue", "BuyingPower", "AvailableFunds",
        "ExcessLiquidity", "MaintMarginReq", "InitMarginReq",
        "GrossPositionValue", "UnrealizedPnL", "RealizedPnL", "Cushion",
        "EquityWithLoanValue", "PreviousDayEquityWithLoanValue",
        "FullInitMarginReq", "FullMaintMarginReq",
    }

    @rate_limit(calls_per_second=1.0)
    async def get_account_summary(self, account: Optional[str] = None) -> Dict:
        """Get account summary.

        Returns a dict shaped:
          {
            "account": "...",
            "as_of": "2026-05-13T...Z",
            "summary": {tag: value, ...}  # only IMPORTANT_ACCOUNT_TAGS
          }

        Bug #1: previous implementation called reqAccountSummaryAsync(group,
        tags) which is no longer the ib_async signature. Use the subscription
        + accountValues() pattern instead.
        """
        try:
            if not await self._ensure_connected():
                return {"error": "Not connected to IBKR"}

            target = account or self.current_account
            if not target and self.ib.managedAccounts():
                target = self.ib.managedAccounts()[0]
            if not target:
                return {"error": "No account available"}

            # Idempotent subscription kick-off. After the first call this is a
            # no-op; values stream in continuously.
            await self._bounded(
                self._ensure_account_summary_subscription(),
                timeout=self.SUMMARY_TIMEOUT,
                op=f"account_summary:{target}",
            )

            values = self.ib.accountValues(target)
            summary = {
                v.tag: v.value
                for v in values
                if v.tag in self.IMPORTANT_ACCOUNT_TAGS
            }

            return {
                "account": target,
                "as_of": dt.datetime.now(dt.timezone.utc).isoformat(),
                "summary": summary,
            }

        except asyncio.TimeoutError:
            return {"error": "IB call timed out fetching account summary; connection reset"}
        except Exception as e:
            self.logger.error(f"Account summary request failed: {e}")
            return {"error": f"IBKR API error: {e}"}

    async def _ensure_account_summary_subscription(self) -> None:
        """Start the account-summary subscription if not already active.

        Idempotent: subsequent calls are cheap no-ops. First call sleeps
        briefly to let the initial snapshot populate before the caller reads
        accountValues().

        Important: must use the *Async* variant. The sync wrapper
        `reqAccountSummary()` internally calls `util.run` which tries to
        start a new event loop on top of the running one and crashes with
        "This event loop is already running". The async version returns a
        coroutine that integrates correctly with the existing loop.
        """
        if getattr(self, "_account_summary_subscribed", False):
            return
        await self.ib.reqAccountSummaryAsync()
        await asyncio.sleep(1.5)  # initial snapshot
        self._account_summary_subscribed = True
    
    @rate_limit(calls_per_second=0.5)
    async def get_shortable_shares(self, symbol: str, account: str = None) -> Dict:
        """Get short-selling availability for a symbol.

        Bug #2 fix: `ib.reqShortableSharesAsync` doesn't exist. The correct
        approach is to subscribe via `reqMktData` with generic tick code 236
        ("Shortable") and poll `ticker.shortableShares`. We poll briefly with
        a hard ceiling and always cancel the subscription afterwards to avoid
        leaks.

        Returns:
          {
            "symbol": "...",
            "shortable_shares": <float|None>,    # IB's log-encoded value
            "classification": "easy_to_borrow" | "hard_to_borrow" | "not_available" | "unknown",
            "current_price": <float>,
            "bid": <float>, "ask": <float>,
            "contract_id": <int>,
            "as_of": "...Z",
          }
        Or {"symbol": ..., "error": "..."}.
        """
        ticker = None
        contract: Optional[Stock] = None
        try:
            if not await self._ensure_connected():
                return {"symbol": symbol, "error": "Not connected to IBKR"}

            contract = Stock(symbol, 'SMART', 'USD')
            qualified = await self._bounded(
                self.ib.qualifyContractsAsync(contract),
                timeout=self.QUALIFY_TIMEOUT,
                op=f"qualify_short:{symbol}",
            )
            if not qualified or not contract.conId:
                return {"symbol": symbol, "error": "Contract not qualifiable"}

            # Generic tick 236 = Shortable. Poll up to 2s for the tick to arrive.
            ticker = self.ib.reqMktData(contract, "236", False, False)
            for _ in range(20):
                if ticker.shortableShares is not None:
                    break
                await asyncio.sleep(0.1)

            shortable_raw = ticker.shortableShares  # log-encoded; see classify

            return {
                "symbol": symbol,
                "shortable_shares": shortable_raw,
                "classification": _classify_shortable(shortable_raw),
                "current_price": safe_float(ticker.last or ticker.close),
                "bid": safe_float(ticker.bid),
                "ask": safe_float(ticker.ask),
                "contract_id": contract.conId,
                "as_of": dt.datetime.now(dt.timezone.utc).isoformat(),
            }

        except asyncio.TimeoutError:
            return {"symbol": symbol, "error": "IB call timed out; connection reset"}
        except Exception as e:
            self.logger.error(f"Error getting shortable shares for {symbol}: {e}")
            return {"symbol": symbol, "error": str(e)}
        finally:
            # Always cancel the subscription so we don't accumulate stale tickers.
            if ticker is not None and contract is not None:
                try:
                    self.ib.cancelMktData(contract)
                except Exception:
                    pass

    async def get_margin_requirements(
        self,
        symbol: str,
        account: str = None,
        test_quantity: int = 100,
    ) -> Dict:
        """Get margin impact for a hypothetical BUY of `test_quantity` shares.

        Bug #3 fix: previous implementation returned a placeholder dict and
        never computed margin. Use `whatIfOrderAsync` which IB exposes
        specifically for "what margin would this order require?" without
        transmitting the order.

        Important: returns *changes* to margin from placing this order, not
        absolute account margin. Field names reflect this. A typical reading:
          - initial_margin_change > 0 means your initial-margin requirement
            would *increase* by that amount if the order filled.
        """
        try:
            if not await self._ensure_connected():
                return {"symbol": symbol, "error": "Not connected to IBKR"}

            contract = Stock(symbol, 'SMART', 'USD')
            qualified = await self._bounded(
                self.ib.qualifyContractsAsync(contract),
                timeout=self.QUALIFY_TIMEOUT,
                op=f"qualify_margin:{symbol}",
            )
            if not qualified or not contract.conId:
                return {"symbol": symbol, "error": "Contract not qualifiable"}

            order = MarketOrder("BUY", test_quantity)
            state = await self._bounded(
                self.ib.whatIfOrderAsync(contract, order),
                timeout=self.DEFAULT_IB_TIMEOUT,
                op=f"whatif:{symbol}",
            )

            return {
                "symbol": symbol,
                "test_quantity": test_quantity,
                "initial_margin_change": safe_float(getattr(state, "initMarginChange", 0)),
                "maintenance_margin_change": safe_float(getattr(state, "maintMarginChange", 0)),
                "equity_with_loan_change": safe_float(getattr(state, "equityWithLoanChange", 0)),
                "commission": safe_float(getattr(state, "commission", 0)),
                "contract_id": contract.conId,
                "exchange": contract.exchange,
                "as_of": dt.datetime.now(dt.timezone.utc).isoformat(),
                "note": "values are CHANGES from a hypothetical BUY, not absolute margin",
            }

        except asyncio.TimeoutError:
            return {"symbol": symbol, "error": "IB call timed out; connection reset"}
        except Exception as e:
            self.logger.error(f"Error getting margin info for {symbol}: {e}")
            return {"symbol": symbol, "error": str(e)}

    async def short_selling_analysis(self, symbols: List[str], account: str = None) -> Dict:
        """Combined short-selling availability + margin analysis.

        Bug #4 fix: previous implementation only caught *raised* exceptions
        and missed errors returned as `{"error": "..."}` from child calls,
        which made `summary.errors` always empty. The new aggregator walks
        both shortable_data and margin_data, surfaces any per-symbol error
        keys, tags them with their source, and adds a `had_errors` boolean
        for one-line client branching.
        """
        try:
            if not await self._ensure_connected():
                return {"error": "Not connected to IBKR"}

            shortable_data: Dict[str, Dict] = {}
            margin_data: Dict[str, Dict] = {}

            for symbol in symbols:
                try:
                    shortable_data[symbol] = await self.get_shortable_shares(symbol, account)
                except Exception as e:
                    shortable_data[symbol] = {"symbol": symbol, "error": str(e)}

            for symbol in symbols:
                try:
                    margin_data[symbol] = await self.get_margin_requirements(symbol, account)
                except Exception as e:
                    margin_data[symbol] = {"symbol": symbol, "error": str(e)}

            # Surface ALL per-symbol errors from the nested dicts (the previous
            # code missed these — it only caught Python exceptions, not
            # error-dict returns from the child handlers).
            errors: List[Dict] = []
            for sym, data in shortable_data.items():
                if isinstance(data, dict) and "error" in data:
                    errors.append({"symbol": sym, "source": "shortable", "error": data["error"]})
            for sym, data in margin_data.items():
                if isinstance(data, dict) and "error" in data:
                    errors.append({"symbol": sym, "source": "margin", "error": data["error"]})

            # Count truly-shortable symbols using the classification.
            shortable_count = sum(
                1 for d in shortable_data.values()
                if isinstance(d, dict)
                and "error" not in d
                and d.get("classification") in ("easy_to_borrow", "hard_to_borrow")
            )

            return {
                "account": account or self.current_account,
                "as_of": dt.datetime.now(dt.timezone.utc).isoformat(),
                "symbols_analyzed": symbols,
                "shortable_data": shortable_data,
                "margin_data": margin_data,
                "summary": {
                    "total_symbols": len(symbols),
                    "shortable_count": shortable_count,
                    "errors": errors,
                    "had_errors": len(errors) > 0,
                },
            }

        except Exception as e:
            self.logger.error(f"Error in short selling analysis: {e}")
            return {"error": str(e)}
    
    async def switch_account(self, account_id: str) -> Dict:
        """Switch to a different IBKR account."""
        try:
            if account_id not in self.accounts:
                self.logger.error(f"Account {account_id} not found. Available: {self.accounts}")
                return {
                    "success": False,
                    "message": f"Account {account_id} not found",
                    "current_account": self.current_account,
                    "available_accounts": self.accounts
                }
            
            self.current_account = account_id
            self.logger.info(f"Switched to account: {account_id}")
            
            return {
                "success": True,
                "message": f"Switched to account: {account_id}",
                "current_account": self.current_account,
                "available_accounts": self.accounts
            }
            
        except Exception as e:
            self.logger.error(f"Error switching account: {e}")
            return {"success": False, "error": str(e)}

    async def get_accounts(self) -> Dict[str, Union[str, List[str]]]:
        """Get available accounts information."""
        try:
            if not await self._ensure_connected():
                await self.connect()
            
            return {
                "current_account": self.current_account,
                "available_accounts": self.accounts,
                "connected": self.is_connected(),
                "paper_trading": self.is_paper
            }
            
        except Exception as e:
            self.logger.error(f"Error getting accounts: {e}")
            return {"error": str(e)}
    
    @rate_limit(calls_per_second=0.5)
    async def get_historical_bars(
        self,
        symbol: str,
        lookback_days: int = 250,
        bar_size: str = "1 day",
    ) -> "pandas.DataFrame":  # type: ignore[name-defined]
        """Daily OHLCV bars for `symbol`, most recent last.

        Used by Layer 2's regime filter. `lookback_days` is calendar days; you'll
        typically get fewer rows back because IBKR skips weekends and holidays.
        """
        if not await self._ensure_connected():
            raise IBKRConnectionError("Not connected to IBKR")

        contract = Stock(symbol, "SMART", "USD")
        await self._bounded(
            self.ib.qualifyContractsAsync(contract),
            timeout=self.QUALIFY_TIMEOUT,
            op=f"qualify_hist:{symbol}",
        )
        if not contract.conId:
            raise RuntimeError(f"Could not qualify contract for {symbol}")

        bars = await self._bounded(
            self.ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                durationStr=f"{lookback_days} D",
                barSizeSetting=bar_size,
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
            ),
            timeout=self.HISTDATA_TIMEOUT,
            op=f"histdata:{symbol}",
        )
        df = util.df(bars)
        if df is None or len(df) == 0:
            raise RuntimeError(f"No historical bars returned for {symbol}")
        return df

    async def get_chart(
        self,
        symbol: str,
        *,
        lookback_days: int = 180,
        sma_periods: tuple[int, ...] = (20, 50),
        theme: str = "dark",
    ) -> Dict:
        """Fetch bars + render a candlestick chart with moving averages.

        Returns a dict shaped for the MCP tool dispatcher to convert
        into a mixed text + ImageContent response. The text block carries
        a short numeric summary so Claude (and a screen-reader user)
        can reason about what the chart shows; the image block is the
        PNG itself.

        Shape:
            {
              "image_png_b64": "<base64>",
              "summary": "AAPL ... last close $XXX (+N%) over M bars",
              "lookback_days": 180,
              "symbol": "AAPL",
              "bars_returned": M,
            }
        """
        from . import charts as _charts
        import base64

        bars = await self.get_historical_bars(symbol, lookback_days=lookback_days)
        if bars is None or len(bars) == 0:
            return {
                "status": "error",
                "symbol": symbol,
                "message": "No historical bars returned",
            }

        # Matplotlib's PNG render is synchronous CPU-bound work. The
        # FIRST call after process start also triggers a font-cache
        # build that can take 5-30 seconds. Doing this on the asyncio
        # event loop would freeze the daemon -- /healthz wouldn't reply,
        # the watchdog would think the daemon is hung, and the in-flight
        # SSE stream to the browser would get torn down mid-response.
        # Offloading to the default thread executor keeps the loop
        # responsive; the chart-requesting handler simply awaits.
        png = await asyncio.to_thread(
            _charts.render_ohlc_chart,
            bars,
            symbol=symbol,
            sma_periods=sma_periods,
            theme=theme,
        )

        last = float(bars["close"].iloc[-1])
        first = float(bars["close"].iloc[0])
        pct = (last - first) / first * 100 if first else 0
        return {
            "status": "ok",
            "symbol": symbol,
            "lookback_days": lookback_days,
            "bars_returned": int(len(bars)),
            "last_close": round(last, 2),
            "pct_change": round(pct, 2),
            "summary": (
                f"{symbol}: ${last:.2f} ({pct:+.1f}% over {len(bars)} bars, "
                f"~{lookback_days} calendar days)"
            ),
            "image_png_b64": base64.b64encode(png).decode("ascii"),
        }

    async def get_swing_visualization(
        self,
        symbol: str,
        *,
        lookback_days: int = 180,
        theme: str = "dark",
    ) -> Dict:
        """Render a candlestick chart with the swing strategy's state overlaid.

        On top of the standard OHLC + moving-averages chart, draws:
          * cost basis (horizontal line, accent color)
          * floor price = cost_basis - floor_offset (horizontal line, red --
            this is where the hard STP fires)
          * current trail-stop estimate = last_close - ATR*trail_multiplier
            (horizontal line, dashed -- moves with price)
          * dip-buy target (only when state == FLAT, since that's when we're
            waiting on a dip to re-enter)
          * last-fill marker (scatter dot at the last buy or sell)

        If the swing strategy isn't registered for ``symbol``, returns
        an error dict; the dispatcher surfaces this as plain text so
        the model can explain to the operator that there's no active
        strategy to visualize.
        """
        from . import charts as _charts
        import base64

        # Need the swing record. Use the same lazy loader the rest of the
        # daemon uses so we don't touch state files on cold start.
        swing_states = self._swing_state_dict()
        state = swing_states.get(symbol)
        if state is None:
            return {
                "status": "error",
                "symbol": symbol,
                "message": f"No active swing strategy for {symbol}. "
                           "Use start_swing_strategy first, or use get_chart "
                           "for a generic price chart.",
            }

        bars = await self.get_historical_bars(symbol, lookback_days=lookback_days)
        if bars is None or len(bars) == 0:
            return {
                "status": "error",
                "symbol": symbol,
                "message": "No historical bars returned",
            }

        # Build the overlay list. Colors picked from the chart theme
        # palette so they read against both light and dark backgrounds.
        overlays: list[dict] = []

        # Cost basis -- always present.
        overlays.append({
            "type": "hline",
            "y": state.cost_basis,
            "label": f"cost ${state.cost_basis:.2f}",
            "color": "#38bdf8",   # accent blue
        })

        # Floor (hard stop). Only meaningful when state is HOLDING --
        # otherwise the STP isn't live -- but we draw it either way so
        # the operator sees the protective level the strategy WOULD use.
        floor = state.cost_basis - state.config.floor_offset
        overlays.append({
            "type": "hline",
            "y": floor,
            "label": f"floor ${floor:.2f}",
            "color": "#f87171",   # down/danger red
        })

        # Trail stop estimate. Needs ATR -- compute from the bars we
        # just fetched (matches the strategy's own per-tick computation
        # at compute_trail_amount in swing.py).
        try:
            from .swing import compute_trail_amount
            trail_amount = compute_trail_amount(
                bars, state.config.trail_atr_multiplier
            )
            last_close = float(bars["close"].iloc[-1])
            trail_stop = last_close - trail_amount
            overlays.append({
                "type": "hline",
                "y": trail_stop,
                "label": f"trail ~${trail_stop:.2f} (ATR×{state.config.trail_atr_multiplier})",
                "color": "#fbbf24",   # warning amber
            })
        except Exception as e:
            # ATR can fail if bars are too short / pandas_ta hiccup --
            # don't let it block the rest of the chart.
            self.logger.warning(f"get_swing_visualization ATR calc failed: {e}")
            trail_stop = None

        # Dip-buy target (only when waiting to re-enter).
        from .swing import SwingState, compute_dip_price
        dip_target = None
        if state.state == SwingState.FLAT and state.last_fill_price:
            try:
                dip_target = compute_dip_price(state.last_fill_price, state.config)
                overlays.append({
                    "type": "hline",
                    "y": dip_target,
                    "label": f"dip target ${dip_target:.2f}",
                    "color": "#10b981",   # up/buy green
                })
            except Exception as e:
                self.logger.warning(f"get_swing_visualization dip calc failed: {e}")

        # Last-fill marker (so the operator can see where we last
        # bought or sold relative to the chart).
        if state.last_fill_time and state.last_fill_price is not None:
            overlays.append({
                "type": "marker",
                "x": state.last_fill_time[:10],   # YYYY-MM-DD
                "y": state.last_fill_price,
                "label": f"last {state.last_fill_action} ${state.last_fill_price:.2f}",
                "color": "#10b981" if state.last_fill_action == "BUY" else "#f87171",
            })

        png = await asyncio.to_thread(
            _charts.render_ohlc_chart,
            bars,
            symbol=symbol,
            sma_periods=(20, 50),
            overlays=overlays,
            theme=theme,
        )

        last = float(bars["close"].iloc[-1])
        return {
            "status": "ok",
            "symbol": symbol,
            "swing_state": state.state.value,
            "quantity": state.quantity,
            "cost_basis": state.cost_basis,
            "floor_price": round(floor, 2),
            "trail_stop_estimate": round(trail_stop, 2) if trail_stop else None,
            "dip_target": round(dip_target, 2) if dip_target else None,
            "last_close": round(last, 2),
            "pct_vs_cost": round((last - state.cost_basis) / state.cost_basis * 100, 2),
            "lookback_days": lookback_days,
            "bars_returned": int(len(bars)),
            "summary": (
                f"{symbol} swing: {state.quantity} shares @ ${state.cost_basis:.2f} "
                f"cost, now ${last:.2f} "
                f"({(last - state.cost_basis) / state.cost_basis * 100:+.1f}%). "
                f"State: {state.state.value}."
            ),
            "image_png_b64": base64.b64encode(png).decode("ascii"),
        }

    async def get_regime_chart(
        self,
        symbol: str,
        *,
        lookback_days: int = 250,
        theme: str = "dark",
        **regime_overrides,
    ) -> Dict:
        """Render a chart annotated with the regime filter's current verdict.

        Shows the price + SMA50 (the period used by the trend gate),
        with the regime classification surfaced in the text summary:
        which of the three gates pass / fail, the underlying numbers
        (ADX, ATR%, SMA slope), and the overall ENABLED / DISABLED
        verdict the trading loop would act on right now.

        Lookback defaults to 250 days because the regime gates have
        warmup requirements (ADX needs 28+, ATR% rolling avg uses 100,
        SMA50 needs 50). 250 gives comfortable headroom + plenty of
        chart history.
        """
        from . import charts as _charts
        from .regime import RegimeConfig, evaluate_gates, aggregate_enabled
        import base64

        bars = await self.get_historical_bars(symbol, lookback_days=lookback_days)
        if bars is None or len(bars) == 0:
            return {
                "status": "error",
                "symbol": symbol,
                "message": "No historical bars returned",
            }

        cfg = RegimeConfig(**regime_overrides) if regime_overrides else RegimeConfig()

        # Evaluate the gates against the same bars we're going to chart.
        try:
            gates = evaluate_gates(bars, cfg)
            enabled = aggregate_enabled(gates, cfg.require_all_gates)
        except ValueError as e:
            # Not enough bars for the regime computation -- still render
            # the chart but note the limitation.
            self.logger.warning(f"regime gates failed for {symbol}: {e}")
            gates = {}
            enabled = None

        # Build a list of which gates failed for the title / overlay label.
        if gates:
            failures = [g for g, info in gates.items() if not info["pass"]]
            verdict_text = "ENABLED" if enabled else "DISABLED"
            if failures and not enabled:
                verdict_text += f" ({', '.join(failures)})"
        else:
            verdict_text = "INSUFFICIENT_DATA"

        # Overlay the SMA50 explicitly via the chart's SMA support; the
        # chart already draws SMA20 + SMA50 by default. We add a "verdict"
        # text overlay by setting it in the title via the summary.
        png = await asyncio.to_thread(
            _charts.render_ohlc_chart,
            bars,
            symbol=f"{symbol}  [{verdict_text}]",
            sma_periods=(20, cfg.sma_period),  # SMA50 by default
            theme=theme,
        )

        last = float(bars["close"].iloc[-1])
        result: Dict = {
            "status": "ok",
            "symbol": symbol,
            "regime_enabled": enabled,
            "verdict": verdict_text,
            "gates": gates,
            "lookback_days": lookback_days,
            "bars_returned": int(len(bars)),
            "last_close": round(last, 2),
            "summary": (
                f"{symbol} regime: {verdict_text}. Last close ${last:.2f}. "
                f"Gates: {sum(g['pass'] for g in gates.values())}/{len(gates)} passing."
                if gates else
                f"{symbol}: insufficient data for regime computation."
            ),
            "image_png_b64": base64.b64encode(png).decode("ascii"),
        }
        return result

    async def get_reversal_visualization(
        self,
        symbol: str,
        *,
        lookback_days: int = 180,
        theme: str = "dark",
    ) -> Dict:
        """Render a chart with reversal-strategy tranche fills overlaid.

        Shows:
          * standard candlestick + SMAs
          * a marker at each filled tranche (green dot, labeled with
            tranche index and fill price)
          * a horizontal line at the average fill price (weighted by
            shares)

        Falls back to a clean error when no active reversal entry
        exists for the symbol; the dispatcher surfaces this so Claude
        can suggest get_chart for a generic view.
        """
        from . import charts as _charts
        import base64

        # Load reversal state via the same lazy initializer the rest of
        # the daemon uses.
        reversal_states = self._reversal_state_dict()
        state = reversal_states.get(symbol)
        if state is None:
            return {
                "status": "error",
                "symbol": symbol,
                "message": f"No active reversal entry for {symbol}. "
                           "Use start_reversal_entry first, or use get_chart "
                           "for a generic price chart.",
            }

        bars = await self.get_historical_bars(symbol, lookback_days=lookback_days)
        if bars is None or len(bars) == 0:
            return {
                "status": "error",
                "symbol": symbol,
                "message": "No historical bars returned",
            }

        overlays: list[dict] = []

        # Per-tranche markers + compute average fill price (share-weighted).
        total_shares = 0
        total_cost = 0.0
        for t in state.filled_tranches:
            overlays.append({
                "type": "marker",
                "x": t.filled_at[:10],
                "y": t.fill_price,
                "label": f"T{t.index} ${t.fill_price:.2f} ({t.shares}sh)",
                "color": "#10b981",   # green (entry)
            })
            total_shares += t.shares
            total_cost += t.shares * t.fill_price

        avg_fill: float | None = None
        if total_shares > 0:
            avg_fill = total_cost / total_shares
            overlays.append({
                "type": "hline",
                "y": avg_fill,
                "label": f"avg fill ${avg_fill:.2f}",
                "color": "#38bdf8",   # accent blue
            })

        png = await asyncio.to_thread(
            _charts.render_ohlc_chart,
            bars,
            symbol=symbol,
            sma_periods=(20, 50),
            overlays=overlays,
            theme=theme,
        )

        last = float(bars["close"].iloc[-1])
        return {
            "status": "ok",
            "symbol": symbol,
            "reversal_status": state.status.value,
            "total_dollars_budget": state.total_dollars,
            "tranches_filled": len(state.filled_tranches),
            "tranches_total": state.config.tranche_count,
            "total_shares": total_shares,
            "total_invested": round(total_cost, 2),
            "average_fill_price": round(avg_fill, 2) if avg_fill else None,
            "remaining_budget": round(state.total_dollars - total_cost, 2),
            "last_close": round(last, 2),
            "unrealized_pnl_pct": (
                round((last - avg_fill) / avg_fill * 100, 2) if avg_fill else None
            ),
            "lookback_days": lookback_days,
            "bars_returned": int(len(bars)),
            "summary": (
                f"{symbol} reversal: {state.status.value}, "
                f"{len(state.filled_tranches)}/{state.config.tranche_count} tranches filled. "
                + (
                    f"Avg fill ${avg_fill:.2f}, now ${last:.2f} "
                    f"({(last - avg_fill) / avg_fill * 100:+.1f}%)."
                    if avg_fill else
                    f"No fills yet. Watching for signals."
                )
            ),
            "image_png_b64": base64.b64encode(png).decode("ascii"),
        }

    async def record_portfolio_snapshot(self) -> Dict:
        """Record one equity snapshot to chat.db.

        Pulls the account summary and inserts a row. Idempotent in the
        sense that you can call this any number of times -- you just
        get more rows. Returns the snapshot fields so the snapshot
        background task can log a brief summary.
        """
        summary = await self.get_account_summary()
        account = summary.get("account") or self.current_account or "unknown"

        # AccountSummary tag names from IBKR. Defaults for missing keys.
        def _num(tag: str) -> Optional[float]:
            v = summary.get(tag)
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        net_liq = _num("NetLiquidation")
        if net_liq is None:
            return {"status": "error", "message": "NetLiquidation missing from account summary"}

        cash = _num("TotalCashValue")
        positions = (net_liq - cash) if (net_liq is not None and cash is not None) else None
        buying = _num("BuyingPower")

        # Lazy import: ChatStore lives in the chat package. We don't
        # depend on it at module-import time so test fixtures can run
        # without the chat package wired up.
        from .chat.persistence import ChatStore
        from .config import settings
        store = ChatStore(settings.chat_db_path)
        store.record_snapshot(
            account=account,
            net_liquidation=net_liq,
            total_cash=cash,
            positions_value=positions,
            buying_power=buying,
        )
        return {
            "status": "ok",
            "account": account,
            "net_liquidation": net_liq,
            "total_cash": cash,
            "positions_value": positions,
            "buying_power": buying,
        }

    async def get_portfolio_equity_curve(
        self,
        *,
        account: Optional[str] = None,
        lookback_days: int = 30,
        theme: str = "dark",
    ) -> Dict:
        """Render the account-equity curve from accumulated snapshots.

        Returns a structured error when fewer than 2 snapshots are
        available -- a single dot isn't useful and the user needs the
        snapshot task to have collected data over time.
        """
        from . import charts as _charts
        from .chat.persistence import ChatStore
        from .config import settings
        import base64

        account = account or self.current_account or "unknown"
        store = ChatStore(settings.chat_db_path)
        snapshots = store.get_snapshots(account=account, lookback_days=lookback_days)

        if len(snapshots) < 2:
            return {
                "status": "error",
                "account": account,
                "snapshots_available": len(snapshots),
                "message": (
                    f"Need at least 2 snapshots for an equity curve; "
                    f"have {len(snapshots)}. The snapshot task runs every "
                    f"{settings.portfolio_snapshot_interval_seconds // 60} min "
                    "by default -- come back after a few hours of data has "
                    "been collected."
                ),
            }

        png = await asyncio.to_thread(
            _charts.render_equity_curve,
            snapshots,
            account=account,
            theme=theme,
        )

        first_v = float(snapshots[0]["net_liquidation"])
        last_v = float(snapshots[-1]["net_liquidation"])
        pct = (last_v - first_v) / first_v * 100 if first_v else 0
        return {
            "status": "ok",
            "account": account,
            "lookback_days": lookback_days,
            "snapshots_in_window": len(snapshots),
            "first_value": round(first_v, 2),
            "last_value": round(last_v, 2),
            "pct_change": round(pct, 2),
            "summary": (
                f"{account} equity: ${last_v:,.0f} ({pct:+.2f}% over "
                f"{len(snapshots)} snapshots, last {lookback_days} days)."
            ),
            "image_png_b64": base64.b64encode(png).decode("ascii"),
        }

    async def _snapshot_loop(self) -> None:
        """Background task: records a portfolio snapshot every
        ``portfolio_snapshot_interval_seconds`` seconds. Started by
        the daemon startup path; exits gracefully on cancellation.

        We DON'T snapshot at startup -- that would record a value
        before the user's morning trading activity settles. The first
        snapshot fires one interval after the daemon starts.
        """
        from .config import settings
        interval = settings.portfolio_snapshot_interval_seconds
        if interval <= 0:
            self.logger.info("portfolio snapshot task disabled (interval=0)")
            return

        self.logger.info(
            f"portfolio snapshot task: recording every {interval}s"
        )
        while True:
            try:
                await asyncio.sleep(interval)
                result = await self.record_portfolio_snapshot()
                if result.get("status") == "ok":
                    self.logger.info(
                        f"portfolio snapshot: ${result['net_liquidation']:,.0f}"
                    )
                else:
                    self.logger.warning(
                        f"portfolio snapshot failed: {result.get('message')}"
                    )
            except asyncio.CancelledError:
                self.logger.info("portfolio snapshot task cancelled")
                return
            except Exception as e:
                # Don't let a transient failure (Gateway hiccup, etc.)
                # crash the background task. Log and try again next tick.
                self.logger.exception(f"portfolio snapshot tick failed: {e}")

    async def check_regime(self, symbol: str, **overrides) -> Dict:
        """Evaluate the Layer 2 regime filter against `symbol`'s recent bars.

        `overrides` are passed to `RegimeConfig` — any subset of `adx_threshold`,
        `atr_lookback`, `sma_period`, `sma_lookback_days`, `require_all_gates`,
        `smoothing_days`.
        """
        try:
            bars = await self.get_historical_bars(symbol, lookback_days=250)
            config = RegimeConfig(**overrides) if overrides else RegimeConfig()
            return check_regime_from_bars(symbol, bars, config)
        except Exception as e:
            self.logger.error(f"check_regime failed for {symbol}: {e}")
            return {"error": str(e), "symbol": symbol}

    # --- Layer 3: reversal entry -------------------------------------------

    def _reversal_state_dict(self) -> Dict[str, ReversalState]:
        """Lazy-load the reversal state file on first access."""
        if self._reversal_states is None:
            self._reversal_states = load_reversal_state(self._reversal_state_path)
        return self._reversal_states

    def _persist_reversal_state(self) -> None:
        save_reversal_state(self._reversal_state_dict(), self._reversal_state_path)

    async def check_reversal_signals(self, symbol: str, **kwargs) -> Dict:
        """Compute the five reversal signals and the recommended tranche.

        Stateless — does NOT touch the reversal entry state machine. Use
        `start_reversal_entry` to actually act on the signals.
        """
        try:
            bars = await self.get_historical_bars(symbol, lookback_days=120)
            min_sig = kwargs.get("min_signals_for_entry", 3)
            return check_reversal_signals_from_bars(symbol, bars, min_sig)
        except Exception as e:
            self.logger.error(f"check_reversal_signals failed for {symbol}: {e}")
            return {"error": str(e), "symbol": symbol}

    async def start_reversal_entry(
        self,
        symbol: str,
        total_dollars: float,
        recheck_interval_seconds: int = 3600,
        **config_kwargs,
    ) -> Dict:
        """Register a reversal entry plan and start the in-process hourly tick.

        Layer 5's daemon will replace the asyncio-task tick with event-driven
        execution; the state shape and `decide_next_action` planner are stable.
        """
        states = self._reversal_state_dict()
        if symbol in states and states[symbol].status in (
            ReversalStatus.WATCHING,
            ReversalStatus.PARTIALLY_FILLED,
            ReversalStatus.STALLED,
        ):
            return {
                "status": "error",
                "symbol": symbol,
                "message": f"reversal entry already running for {symbol} (status={states[symbol].status.value})",
            }

        # Build config from known kwargs only
        valid = {f for f in ReversalConfig.__dataclass_fields__}
        cfg = ReversalConfig(**{k: v for k, v in config_kwargs.items() if k in valid})

        today = dt.date.today().isoformat()
        states[symbol] = ReversalState(
            symbol=symbol,
            total_dollars=total_dollars,
            config=cfg,
            started_at=today,
            last_action_at=today,
        )
        self._persist_reversal_state()

        if symbol not in self._reversal_tasks or self._reversal_tasks[symbol].done():
            self._reversal_tasks[symbol] = asyncio.create_task(
                self._reversal_loop(symbol, recheck_interval_seconds)
            )

        return {
            "status": "started",
            "symbol": symbol,
            "total_dollars": total_dollars,
            "tranche_count": cfg.tranche_count,
            "tranche_sizing": cfg.tranche_sizing,
            "min_signals_for_entry": cfg.min_signals_for_entry,
            "recheck_interval_seconds": recheck_interval_seconds,
        }

    async def stop_reversal_entry(
        self,
        symbol: str,
        action: str = "cancel",
        confirm: bool = False,
    ) -> Dict:
        """Stop a running reversal entry.

        `action`:
          - "cancel": stop the tick, leave filled tranches alone
          - "liquidate_filled": stop the tick AND market-sell everything filled
          - "convert_to_swing_loop": hands filled tranches to swing loop

        When env var REQUIRE_CONFIRMATION_FOR_DESTRUCTIVE_TOOLS is on, this
        returns a `needs_confirmation` preview unless called with confirm=True.
        """
        states = self._reversal_state_dict()
        state = states.get(symbol)
        if not state:
            return {"status": "error", "message": f"no reversal entry for {symbol}"}

        # Destructive-tool gate. Surface what would happen for each action.
        if self._needs_confirmation(confirm):
            filled_count = len(state.filled_tranches)
            filled_shares = sum(t.shares for t in state.filled_tranches)
            if action == "cancel":
                impact = (
                    f"would stop watching for {symbol} signals; "
                    f"{filled_count} filled tranche(s) ({filled_shares} shares) left alone"
                )
            elif action == "liquidate_filled":
                impact = (
                    f"would stop watching AND market-sell {filled_shares} "
                    f"shares across {filled_count} filled tranche(s)"
                )
            elif action == "convert_to_swing_loop":
                impact = (
                    f"would hand {filled_shares} shares to swing strategy management"
                )
            else:
                impact = f"unknown action: {action}"
            return self._confirm_response(
                action=f"stop_reversal_entry:{action}",
                preview={
                    "symbol": symbol,
                    "current_status": state.status.value,
                    "filled_tranches": filled_count,
                    "filled_shares": filled_shares,
                    "would_do": impact,
                },
                hint=(
                    f"REQUIRE_CONFIRMATION_FOR_DESTRUCTIVE_TOOLS is on. {impact}. "
                    "Re-call with confirm=true to proceed."
                ),
            )

        task = self._reversal_tasks.pop(symbol, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        if action == "cancel":
            state.status = ReversalStatus.CANCELLED
            self._persist_reversal_state()
            return {
                "status": "cancelled",
                "symbol": symbol,
                "filled_tranches": len(state.filled_tranches),
            }

        if action == "liquidate_filled":
            total_shares = sum(t.shares for t in state.filled_tranches)
            sell_result: Dict[str, Any] = {"status": "noop", "message": "no filled shares"}
            if total_shares > 0:
                sell_result = await self.place_order(
                    symbol=symbol, action="SELL", quantity=total_shares,
                    order_type="MKT", tif="GTC",
                )
            state.status = ReversalStatus.LIQUIDATED
            self._persist_reversal_state()
            return {
                "status": "liquidated",
                "symbol": symbol,
                "filled_tranches": len(state.filled_tranches),
                "shares_sold": total_shares,
                "sell_order": sell_result,
            }

        if action == "convert_to_swing_loop":
            if not state.filled_tranches:
                return {
                    "status": "error",
                    "symbol": symbol,
                    "message": "no filled tranches to convert",
                }
            total_shares = sum(t.shares for t in state.filled_tranches)
            avg_cost = sum(t.shares * t.fill_price for t in state.filled_tranches) / total_shares
            # Hand off with default swing config; caller can refine via
            # update_swing_params after the handoff.
            handoff_result = await self.start_swing_strategy(
                symbol=symbol,
                quantity=total_shares,
                cost_basis=round(avg_cost, 2),
                dip_percent=3.0,           # conservative default re-entry
                trail_atr_multiplier=2.0,
            )
            state.status = ReversalStatus.COMPLETE
            self._persist_reversal_state()
            return {
                "status": "converted",
                "symbol": symbol,
                "handoff": {"shares": total_shares, "cost_basis": round(avg_cost, 2)},
                "swing": handoff_result,
            }

        return {"status": "error", "message": f"unknown action: {action}"}

    # --- Layer 4: swing-trading loop ---------------------------------------

    def _swing_state_dict(self) -> Dict[str, SwingStateRecord]:
        if self._swing_states is None:
            self._swing_states = load_swing_state(self._swing_state_path)
        return self._swing_states

    def _persist_swing_state(self) -> None:
        save_swing_state(self._swing_state_dict(), self._swing_state_path)

    async def start_swing_strategy(
        self,
        symbol: str,
        quantity: int,
        cost_basis: float,
        recheck_interval_seconds: int = 3600,
        **config_kwargs: Any,
    ) -> Dict:
        """Register a swing strategy and start the in-process hourly tick."""
        if quantity <= 0:
            return {"status": "error", "message": "quantity must be > 0"}
        if cost_basis <= 0:
            return {"status": "error", "message": "cost_basis must be > 0"}

        valid = {f for f in SwingConfig.__dataclass_fields__}
        cfg = SwingConfig(**{k: v for k, v in config_kwargs.items() if k in valid})

        # Validate dip_amount XOR dip_percent
        has_amt = cfg.dip_amount is not None
        has_pct = cfg.dip_percent is not None
        if has_amt == has_pct:
            return {
                "status": "error",
                "message": "specify exactly one of dip_amount or dip_percent",
            }

        states = self._swing_state_dict()
        if symbol in states and states[symbol].state is not SwingState.STOPPED:
            return {
                "status": "error",
                "symbol": symbol,
                "message": f"swing strategy already active for {symbol} "
                           f"(state={states[symbol].state.value})",
            }

        now = dt.datetime.now(dt.timezone.utc).isoformat()
        states[symbol] = SwingStateRecord(
            symbol=symbol,
            quantity=int(quantity),
            cost_basis=float(cost_basis),
            config=cfg,
            state=SwingState.HOLDING,
            started_at=now,
            last_tick_at=now,
        )
        self._persist_swing_state()

        if symbol not in self._swing_tasks or self._swing_tasks[symbol].done():
            self._swing_tasks[symbol] = asyncio.create_task(
                self._swing_loop(symbol, recheck_interval_seconds)
            )

        return {
            "status": "started",
            "symbol": symbol,
            "quantity": quantity,
            "cost_basis": cost_basis,
            "config": asdict(cfg),
            "recheck_interval_seconds": recheck_interval_seconds,
        }

    async def stop_swing_strategy(self, symbol: str, confirm: bool = False) -> Dict:
        """Cancel any open swing orders and stop the loop.

        When env var REQUIRE_CONFIRMATION_FOR_DESTRUCTIVE_TOOLS is on, this
        returns a `needs_confirmation` preview unless called with confirm=True.
        Designed to prevent chat sessions from cancelling protective stops
        without an explicit second step.
        """
        states = self._swing_state_dict()
        state = states.get(symbol)
        if not state:
            return {"status": "error", "message": f"no swing strategy for {symbol}"}

        # Destructive-tool gate: stopping cancels live protective orders.
        if self._needs_confirmation(confirm):
            order_ids = [
                getattr(state, f)
                for f in ("protective_trail_order_id", "protective_stop_order_id", "dip_buy_order_id")
                if getattr(state, f)
            ]
            return self._confirm_response(
                action="stop_swing_strategy",
                preview={
                    "symbol": symbol,
                    "current_state": state.state.value,
                    "quantity": state.quantity,
                    "cost_basis": state.cost_basis,
                    "orders_that_would_be_cancelled": order_ids,
                    "oca_group": state.oca_group,
                },
                hint=(
                    f"REQUIRE_CONFIRMATION_FOR_DESTRUCTIVE_TOOLS is on. "
                    f"This would cancel {len(order_ids)} protective order(s) "
                    f"({order_ids}) on {symbol} ({state.quantity} shares) "
                    "and stop the swing loop. "
                    "Re-call with confirm=true to proceed."
                ),
            )

        task = self._swing_tasks.pop(symbol, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        # Cancel any open orders we know about
        cancelled: list[int] = []
        for oid_field in (
            "protective_trail_order_id",
            "protective_stop_order_id",
            "dip_buy_order_id",
        ):
            oid = getattr(state, oid_field)
            if not oid or not self.ib:
                continue
            for trade in self.ib.trades():
                if trade.order.orderId == oid and trade.orderStatus.status in (
                    "PreSubmitted", "Submitted", "PendingSubmit"
                ):
                    self.ib.cancelOrder(trade.order)
                    cancelled.append(oid)
                    break
            setattr(state, oid_field, None)

        state.state = SwingState.STOPPED
        self._persist_swing_state()
        return {
            "status": "stopped",
            "symbol": symbol,
            "cancelled_order_ids": cancelled,
            "last_state": state.state.value,
        }

    async def get_swing_status(self, symbol: str) -> Dict:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        state = self._swing_state_dict().get(symbol)
        if not state:
            return {"status": "not_found", "symbol": symbol, "as_of": now}
        return {
            "symbol": symbol,
            "state": state.state.value,
            "quantity": state.quantity,
            "cost_basis": state.cost_basis,
            "config": asdict(state.config),
            "protective_trail_order_id": state.protective_trail_order_id,
            "protective_stop_order_id": state.protective_stop_order_id,
            "oca_group": state.oca_group,
            "dip_buy_order_id": state.dip_buy_order_id,
            "last_fill_action": state.last_fill_action,
            "last_fill_price": state.last_fill_price,
            "last_fill_time": state.last_fill_time,
            "last_regime_enabled": state.last_regime_enabled,
            "started_at": state.started_at,
            "last_tick_at": state.last_tick_at,
            "as_of": now,
        }

    # Parameters that affect *live* orders. Changing any of these means the
    # active OCA pair (HOLDING) or dip-buy (FLAT) no longer matches user intent
    # and must be cancelled so the next tick re-places it with new values.
    _STRUCTURAL_SWING_PARAMS = {
        "trail_atr_multiplier",
        "floor_offset",
        "dip_amount",
        "dip_percent",
    }

    async def update_swing_params(self, symbol: str, **params: Any) -> Dict:
        """Update swing config and reconcile broker-side orders.

        If any STRUCTURAL parameter changed (one that affects the live OCA or
        dip-buy), the corresponding orders are cancelled and a tick is fired
        immediately so the new params reach the broker within a second.
        Non-structural params (regime_filter_enabled, cooldown_hours, etc.)
        take effect on the next regularly-scheduled tick.

        When env var REQUIRE_CONFIRMATION_FOR_DESTRUCTIVE_TOOLS is on, a
        structural change requires `confirm=True` because it cancels live
        protective orders. Non-structural changes don't require confirm
        even when the gate is on (they don't touch live orders).
        """
        # Pull confirm out so it doesn't get applied to SwingConfig fields.
        confirm = bool(params.pop("confirm", False))

        state = self._swing_state_dict().get(symbol)
        if not state:
            return {"status": "error", "message": f"no swing strategy for {symbol}"}

        # Pre-compute what would change so we can decide whether to gate.
        valid = {f for f in SwingConfig.__dataclass_fields__}
        proposed = {k: v for k, v in params.items() if k in valid}
        proposed_structural = set(proposed.keys()) & self._STRUCTURAL_SWING_PARAMS

        # Destructive-tool gate — only for structural changes that would cancel
        # live broker orders. Non-structural updates aren't gated.
        if proposed_structural and self._needs_confirmation(confirm):
            live_order_ids = [
                getattr(state, f)
                for f in ("protective_trail_order_id", "protective_stop_order_id", "dip_buy_order_id")
                if getattr(state, f)
            ]
            return self._confirm_response(
                action="update_swing_params:structural",
                preview={
                    "symbol": symbol,
                    "structural_changes": {k: proposed[k] for k in proposed_structural},
                    "non_structural_changes": {
                        k: proposed[k] for k in proposed if k not in proposed_structural
                    },
                    "live_orders_that_would_be_cancelled": live_order_ids,
                },
                hint=(
                    "REQUIRE_CONFIRMATION_FOR_DESTRUCTIVE_TOOLS is on. "
                    f"Structural change would cancel {len(live_order_ids)} live order(s) "
                    f"on {symbol} and re-place them with new parameters. "
                    "Re-call with confirm=true to proceed."
                ),
            )

        applied: Dict[str, Any] = {}
        for k, v in proposed.items():
            setattr(state.config, k, v)
            applied[k] = v

        structural_changed = bool(set(applied.keys()) & self._STRUCTURAL_SWING_PARAMS)
        cancelled: list[int] = []

        if structural_changed and self.ib and self.is_connected():
            # Cancel any live orders that were built with the OLD config.
            for field in (
                "protective_trail_order_id",
                "protective_stop_order_id",
                "dip_buy_order_id",
            ):
                oid = getattr(state, field)
                if not oid:
                    continue
                for trade in self.ib.trades():
                    if (
                        trade.order.orderId == oid
                        and trade.orderStatus.status in (
                            "PreSubmitted", "Submitted", "PendingSubmit"
                        )
                    ):
                        self.ib.cancelOrder(trade.order)
                        cancelled.append(oid)
                        break
                setattr(state, field, None)
            state.oca_group = None

        self._persist_swing_state()

        # Fire an immediate tick so the new params reach the broker now.
        if structural_changed:
            asyncio.create_task(self._swing_tick(symbol))

        return {
            "status": "updated",
            "symbol": symbol,
            "applied": applied,
            "structural_changed": structural_changed,
            "cancelled_order_ids": cancelled,
            "note": (
                "Structural change — old orders cancelled, new ones will be placed by the immediate tick."
                if structural_changed else
                "Non-structural change — takes effect on next tick."
            ),
        }

    async def tick_now(self, symbol: str, kind: str = "swing") -> Dict:
        """Force an immediate strategy tick. Useful for testing or after a
        manual config change. `kind` is "swing" or "reversal"."""
        if kind == "swing":
            if symbol not in self._swing_state_dict():
                return {"status": "error", "message": f"no swing strategy for {symbol}"}
            return await self._swing_tick(symbol)
        if kind == "reversal":
            if symbol not in self._reversal_state_dict():
                return {"status": "error", "message": f"no reversal entry for {symbol}"}
            return await self._reversal_tick(symbol)
        return {"status": "error", "message": f"unknown kind: {kind}"}

    async def _swing_loop(self, symbol: str, interval_seconds: int) -> None:
        try:
            while True:
                try:
                    await self._swing_tick(symbol)
                except Exception as e:
                    self.logger.exception(f"swing tick failed for {symbol}: {e}")
                await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            self.logger.info(f"swing loop for {symbol} cancelled")
            raise

    async def _swing_tick(self, symbol: str) -> Dict:
        states = self._swing_state_dict()
        state = states.get(symbol)
        if not state or state.state is SwingState.STOPPED:
            return {"action": "stopped"}

        now = dt.datetime.now(dt.timezone.utc)
        bars = await self.get_historical_bars(symbol, lookback_days=120)

        # 1. Detect fills since last tick
        trades = list(self.ib.trades()) if self.ib else []
        fills = detect_fills_from_trades(state, trades)
        for fill in fills:
            apply_fill(state, fill)

        # 2. Optional regime check (uses 250d of bars — separate call)
        regime_enabled = True
        if state.config.regime_filter_enabled:
            try:
                regime_bars = await self.get_historical_bars(symbol, lookback_days=250)
                from .regime import check_regime_from_bars as _crfb
                regime_result = _crfb(symbol, regime_bars)
                regime_enabled = regime_result.get("enabled", True)
                state.last_regime_enabled = regime_enabled
            except Exception as e:
                self.logger.warning(
                    f"swing tick regime check failed for {symbol}: {e}"
                )

        # 3. Plan
        decision = swing_decide_next_action(state, bars, regime_enabled, now)
        action = decision["action"]

        # 4. Execute
        if action == "place_protective_oca":
            grp = make_group_id(f"swing-{symbol.lower()}")
            result = await self.place_oca_group(
                oca_group_name=grp,
                orders=[
                    {"symbol": symbol, "action": "SELL", "quantity": state.quantity,
                     "order_type": "TRAIL", "trail_amount": decision["trail_amount"],
                     "tif": "GTC"},
                    {"symbol": symbol, "action": "SELL", "quantity": state.quantity,
                     "order_type": "STP", "stop_price": decision["floor_price"],
                     "tif": "GTC"},
                ],
            )
            if result["status"] == "submitted" and len(result.get("orders", [])) == 2:
                state.protective_trail_order_id = result["orders"][0].get("order_id")
                state.protective_stop_order_id = result["orders"][1].get("order_id")
                state.oca_group = grp

        elif action == "place_dip_buy":
            result = await self.place_order(
                symbol=symbol, action="BUY", quantity=decision["quantity"],
                order_type="LMT", limit_price=decision["limit_price"], tif="GTC",
            )
            if result["status"] == "submitted":
                state.dip_buy_order_id = result.get("order_id")

        elif action == "cancel_dip_buy":
            if state.dip_buy_order_id and self.ib:
                for trade in trades:
                    if trade.order.orderId == state.dip_buy_order_id:
                        self.ib.cancelOrder(trade.order)
                        break
            state.dip_buy_order_id = None

        state.last_tick_at = now.isoformat()
        self._persist_swing_state()
        return {
            "action": action,
            "fills": [{"role": f.role, "price": f.fill_price} for f in fills],
            "decision": {k: v for k, v in decision.items() if k != "action"},
            "regime_enabled": regime_enabled,
        }

    async def get_reversal_status(self, symbol: str) -> Dict:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        states = self._reversal_state_dict()
        state = states.get(symbol)
        if not state:
            return {"status": "not_found", "symbol": symbol, "as_of": now}
        return {
            "symbol": symbol,
            "status": state.status.value,
            "total_dollars": state.total_dollars,
            "config": asdict(state.config),
            "last_signal_count": state.last_signal_count,
            "last_signal_dict": state.last_signal_dict,
            "consecutive_days_at_threshold": state.consecutive_days_at_threshold,
            "filled_tranches": [
                {
                    "index": t.index, "shares": t.shares,
                    "target_dollars": t.target_dollars,
                    "fill_price": t.fill_price, "filled_at": t.filled_at,
                }
                for t in state.filled_tranches
            ],
            "started_at": state.started_at,
            "last_action_at": state.last_action_at,
            "protective_stop_order_id": state.protective_stop_order_id,
            "as_of": now,
        }

    async def _reversal_loop(self, symbol: str, interval_seconds: int) -> None:
        """Background task: call _reversal_tick on interval until cancelled."""
        try:
            while True:
                try:
                    await self._reversal_tick(symbol)
                except Exception as e:
                    self.logger.exception(f"reversal tick failed for {symbol}: {e}")
                await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            self.logger.info(f"reversal loop for {symbol} cancelled")
            raise

    async def _reversal_tick(self, symbol: str) -> Dict:
        """One iteration: fetch bars, plan action, execute, persist."""
        states = self._reversal_state_dict()
        state = states.get(symbol)
        if not state:
            return {"action": "missing"}
        if state.status not in (
            ReversalStatus.WATCHING,
            ReversalStatus.PARTIALLY_FILLED,
            ReversalStatus.STALLED,
        ):
            return {"action": "terminal", "status": state.status.value}

        bars = await self.get_historical_bars(symbol, lookback_days=120)
        today = dt.date.today()
        decision = decide_next_action(state, bars, today)

        # Update state with the latest reading regardless of action
        state.last_signal_count = decision["signal_count"]
        state.last_signal_dict = decision["signals"]
        state.consecutive_days_at_threshold = decision["consecutive_days_at_threshold"]
        state.last_check_date = today.isoformat()

        action = decision["action"]
        if action == "place_tranche":
            idx = decision["tranche_index"]
            target = decision["target_dollars"]
            price_now = float(bars["close"].iloc[-1])
            shares = int(target / price_now)
            if shares <= 0:
                self.logger.warning(
                    f"reversal {symbol}: tranche {idx} target ${target:.2f} at "
                    f"${price_now:.2f} rounds to 0 shares; holding"
                )
                self._persist_reversal_state()
                return {"action": "hold", "reason": "zero_shares"}

            result = await self.place_order(
                symbol=symbol, action="BUY", quantity=shares,
                order_type="MKT", tif="GTC",
            )
            if result["status"] == "submitted":
                state.filled_tranches.append(FilledTranche(
                    index=idx,
                    target_dollars=target,
                    shares=shares,
                    fill_price=price_now,  # approximation; real fill via event in Layer 5
                    filled_at=today.isoformat(),
                ))
                if idx >= state.config.tranche_count:
                    state.status = ReversalStatus.COMPLETE
                else:
                    state.status = ReversalStatus.PARTIALLY_FILLED
                state.last_action_at = today.isoformat()
            self._persist_reversal_state()
            return {"action": action, "result": result, "tranche_index": idx}

        if action == "place_protective_stop":
            if not state.filled_tranches:
                return {"action": "hold", "reason": "no_tranches_to_protect"}
            last = state.filled_tranches[-1]
            result = await self.place_order(
                symbol=symbol, action="SELL", quantity=last.shares,
                order_type="STP", stop_price=decision["stop_price"], tif="GTC",
            )
            if result["status"] == "submitted":
                state.protective_stop_order_id = result.get("order_id")
                state.status = ReversalStatus.STALLED
                state.last_action_at = today.isoformat()
            self._persist_reversal_state()
            return {"action": action, "result": result}

        if action == "abort_stalled":
            state.status = ReversalStatus.ABORTED
            self._persist_reversal_state()
            return {"action": action, "days_since": decision["days_since_last_action"]}

        if action == "complete":
            state.status = ReversalStatus.COMPLETE
            self._persist_reversal_state()
            return {"action": action}

        self._persist_reversal_state()
        return {"action": "hold", "signal_count": decision["signal_count"]}

    async def place_order(self, **kwargs) -> Dict:
        """Place a single order. Honors `ENABLE_LIVE_TRADING` and `MAX_ORDER_SIZE`.

        All Layer 1 order types are supported (MKT, LMT, STP, STP LMT, TRAIL,
        TRAIL LIMIT, LOO, MOO, LOC, MOC). See `orders.py` for validation rules.

        When `REQUIRE_CONFIRMATION_FOR_DESTRUCTIVE_TOOLS` env var is set,
        non-dry-run calls return a `needs_confirmation` preview unless
        called with `confirm=True`.
        """
        # Strip confirm before passing the rest to OrderRequest (which would
        # reject it as an unknown parameter).
        confirm = bool(kwargs.pop("confirm", False))

        try:
            req = OrderRequest.from_kwargs(**kwargs)
            validate_request(req)
        except ValidationError as e:
            return self._order_response("error", req=None, preview_kwargs=kwargs, message=str(e))

        preview = make_preview(req)

        if not settings.enable_live_trading:
            return self._order_response(
                "blocked",
                req=req,
                preview=preview,
                message="ENABLE_LIVE_TRADING=false; order not transmitted",
            )

        if req.dry_run:
            return self._order_response(
                "dry_run",
                req=req,
                preview=preview,
                message="dry_run=true; order not transmitted",
            )

        # Destructive-tool gate: live-transmission requires explicit confirm.
        if self._needs_confirmation(confirm):
            return self._confirm_response(
                action="place_order",
                preview=preview,
                hint=(
                    f"REQUIRE_CONFIRMATION_FOR_DESTRUCTIVE_TOOLS is on. "
                    f"This would {preview.get('intent', 'place an order')}. "
                    "Re-call with confirm=true to actually transmit."
                ),
            )

        if not await self._ensure_connected():
            return self._order_response(
                "error",
                req=req,
                preview=preview,
                message="Not connected to IBKR",
            )

        try:
            # Serialize order placements so a slow one can't block a parallel
            # order. Reads are unaffected (no lock acquired by read endpoints).
            async with self._order_lock:
                contract = Stock(req.symbol, "SMART", "USD")
                qualified = await self._bounded(
                    self.ib.qualifyContractsAsync(contract),
                    timeout=self.QUALIFY_TIMEOUT,
                    op=f"qualify:{req.symbol}",
                )
                if not qualified or not contract.conId:
                    return self._order_response(
                        "error",
                        req=req,
                        preview=preview,
                        message=f"Could not qualify contract for {req.symbol}",
                    )

                ib_order = build_order(req)
                # placeOrder() is synchronous in ib_async — returns a Trade
                # immediately. No await needed; no timeout needed for this line.
                trade = self.ib.placeOrder(contract, ib_order)

            # Per-order ntfy in live mode (best-effort). The existing
            # _on_fill_for_chat handler pushes the FILL into chat; this
            # handler pushes the SUBMIT to the operator's phone so they
            # know an order just hit IBKR in real money. Skip in paper.
            try:
                from . import live_safety
                from .notify import send as _ntfy_send
                if (live_safety.is_live_mode()
                        and settings.live_ntfy_every_order):
                    side_emoji = "🟢" if req.action == "BUY" else "🔴"
                    px_str = (
                        f"@ ${req.limit_price:.2f}" if req.limit_price
                        else f"({req.order_type})"
                    )
                    _ntfy_send(
                        title=(
                            f"{side_emoji} LIVE order submitted: "
                            f"{req.action} {req.quantity} {req.symbol}"
                        ),
                        message=(
                            f"{req.order_type} {px_str} "
                            f"· order #{getattr(trade.order, 'orderId', '?')}"
                        ),
                        priority=4,
                        tags=["money_with_wings"],
                    )
            except Exception:
                pass  # don't let ntfy errors fail the order response

            return self._order_response(
                "submitted",
                req=req,
                preview=preview,
                order_id=getattr(trade.order, "orderId", None),
                perm_id=getattr(trade.order, "permId", None) or None,
                message="Order submitted",
            )
        except asyncio.TimeoutError:
            return self._order_response(
                "error",
                req=req,
                preview=preview,
                message=(
                    f"IB call timed out placing {req.order_type} on {req.symbol}; "
                    "connection has been reset"
                ),
            )
        except Exception as e:
            self.logger.error(f"place_order failed for {req.symbol}: {e}")
            return self._order_response(
                "error",
                req=req,
                preview=preview,
                message=f"IBKR API error: {e}",
            )

    async def place_oca_group(
        self,
        orders: List[Dict],
        oca_group_name: str,
        oca_type: int = 1,
        dry_run: bool = False,
        confirm: bool = False,
    ) -> Dict:
        """Place 2+ linked orders that cancel each other on any fill.

        `dry_run` (Phase 3 polish): when True at the group level, validates
        every leg and returns a preview without any IB call.

        `confirm` (destructive-tool gate): when env var
        REQUIRE_CONFIRMATION_FOR_DESTRUCTIVE_TOOLS is on, non-dry-run calls
        require confirm=True to actually transmit.
        """
        try:
            requests = [OrderRequest.from_kwargs(**o) for o in orders]
            prepared = prepare_group(requests, group_name=oca_group_name, oca_type=oca_type)
        except ValidationError as e:
            return {
                "status": "error",
                "group_id": oca_group_name,
                "orders": [],
                "message": str(e),
            }

        previews = [make_preview(r) for r in prepared]

        if not settings.enable_live_trading:
            return {
                "status": "blocked",
                "group_id": oca_group_name,
                "orders": [{"preview": p, "order_id": None, "perm_id": None} for p in previews],
                "message": "ENABLE_LIVE_TRADING=false; OCA group not transmitted",
            }

        # Group-level dry_run OR any leg with dry_run=True triggers preview mode.
        if dry_run or any(r.dry_run for r in prepared):
            return {
                "status": "dry_run",
                "group_id": oca_group_name,
                "orders": [{"preview": p, "order_id": None, "perm_id": None} for p in previews],
                "message": "dry_run; OCA group not transmitted",
            }

        # Destructive-tool gate.
        if self._needs_confirmation(confirm):
            return self._confirm_response(
                action="place_oca_group",
                preview={
                    "group_id": oca_group_name,
                    "legs": previews,
                },
                hint=(
                    "REQUIRE_CONFIRMATION_FOR_DESTRUCTIVE_TOOLS is on. "
                    f"This would transmit an OCA group of {len(previews)} linked orders. "
                    "Re-call with confirm=true to actually transmit."
                ),
            )

        if not await self._ensure_connected():
            return {
                "status": "error",
                "group_id": oca_group_name,
                "orders": [{"preview": p, "order_id": None, "perm_id": None} for p in previews],
                "message": "Not connected to IBKR",
            }

        results: List[Dict] = []
        try:
            # Same lock as place_order: group submission is serialized so two
            # concurrent group placements can't interleave and confuse the
            # OCA-cancellation rules at the broker.
            async with self._order_lock:
                for req, preview in zip(prepared, previews):
                    contract = Stock(req.symbol, "SMART", "USD")
                    qualified = await self._bounded(
                        self.ib.qualifyContractsAsync(contract),
                        timeout=self.QUALIFY_TIMEOUT,
                        op=f"qualify:{req.symbol}",
                    )
                    if not qualified or not contract.conId:
                        return {
                            "status": "error",
                            "group_id": oca_group_name,
                            "orders": results,
                            "message": f"Could not qualify contract for {req.symbol}",
                        }
                    ib_order = build_order(req)
                    trade = self.ib.placeOrder(contract, ib_order)
                    results.append(
                        {
                            "preview": preview,
                            "order_id": getattr(trade.order, "orderId", None),
                            "perm_id": getattr(trade.order, "permId", None) or None,
                        }
                    )
            return {
                "status": "submitted",
                "group_id": oca_group_name,
                "orders": results,
                "message": f"OCA group of {len(results)} orders submitted",
            }
        except asyncio.TimeoutError:
            return {
                "status": "error",
                "group_id": oca_group_name,
                "orders": results,
                "message": "IB call timed out placing OCA group; connection has been reset",
            }
        except Exception as e:
            self.logger.error(f"place_oca_group failed: {e}")
            return {
                "status": "error",
                "group_id": oca_group_name,
                "orders": results,
                "message": f"IBKR API error: {e}",
            }

    def _order_response(
        self,
        status: str,
        req: Optional[OrderRequest],
        preview: Optional[Dict] = None,
        preview_kwargs: Optional[Dict] = None,
        order_id: Optional[int] = None,
        perm_id: Optional[int] = None,
        message: str = "",
    ) -> Dict:
        """Uniform response shape across all `place_order` paths."""
        if preview is None:
            preview = preview_kwargs or {}
        return {
            "status": status,
            "order_id": order_id,
            "perm_id": perm_id,
            "preview": preview,
            "message": message,
        }

    # --- Layer 5a: daemon-mode resilience ---------------------------------

    def _on_exec_details_for_strategies(self, trade, fill) -> None:
        """Real-time fill detection: when IBKR fires execDetailsEvent for an
        order we're tracking, immediately schedule a strategy tick so we react
        within milliseconds instead of waiting up to an hour for the next poll.
        """
        order_id = getattr(trade.order, "orderId", None)
        if order_id is None:
            return

        # Match against reversal strategies
        for symbol, state in self._reversal_state_dict().items():
            if order_id == state.protective_stop_order_id:
                self.logger.info(
                    f"reversal {symbol}: fill event on protective stop {order_id}, triggering tick"
                )
                asyncio.create_task(self._reversal_tick(symbol))
                return

        # Match against swing strategies
        for symbol, state in self._swing_state_dict().items():
            tracked = (
                state.protective_trail_order_id,
                state.protective_stop_order_id,
                state.dip_buy_order_id,
            )
            if order_id in tracked:
                self.logger.info(
                    f"swing {symbol}: fill event on order {order_id}, triggering tick"
                )
                asyncio.create_task(self._swing_tick(symbol))
                return

    def _on_fill_for_chat(self, trade, fill) -> None:
        """Forward every execution to the operator's active chat thread
        as a synthetic 'system fill' message.

        Fired by ib_async on execDetails. Runs in the ib event loop
        (sync); we schedule the SQLite write + SSE publish as an
        asyncio task so the event handler returns immediately.

        Defensive against every external failure -- a missing
        activeThreadId pref, a stale thread, an SSE publish error -- so
        a chat-bridge hiccup never affects order routing or strategy
        ticks.
        """
        try:
            exec_ = getattr(fill, "execution", None)
            contract = getattr(trade, "contract", None)
            order = getattr(trade, "order", None)
            if exec_ is None or contract is None or order is None:
                return
            symbol = getattr(contract, "symbol", "?")
            side = getattr(order, "action", "?")
            qty = int(getattr(exec_, "shares", 0) or 0)
            price = float(getattr(exec_, "price", 0) or 0)
            order_id = getattr(order, "orderId", 0)
            # IB times in YYYYMMDD hh:mm:ss + sometimes a timezone suffix.
            t = getattr(exec_, "time", None)
            try:
                time_str = t.strftime("%H:%M:%S") if t else "??:??:??"
            except Exception:
                time_str = str(t)[:8] if t else "??:??:??"
            marker = "🟢" if side == "BUY" else "🔴"
            msg = (
                f"{marker} FILL · {side} {qty} {symbol} @ ${price:.2f} "
                f"· {time_str} · order #{order_id}"
            )
            # Schedule the async write -- the event handler is sync.
            asyncio.create_task(self._append_fill_to_chat(msg, order_id))
        except Exception as e:
            self.logger.warning(f"chat fill handler failed: {e}")

    async def _append_fill_to_chat(self, msg: str, order_id: int) -> None:
        """Find the active thread, append the synthetic message, fire SSE.

        Written as a `user` message (not `assistant`) so Claude on the
        next turn sees it as context but doesn't treat it as its own
        prior utterance to continue. The visible '🟢 FILL' prefix makes
        it obviously not-the-operator to the human reader too.
        """
        try:
            from .chat.routes import _get_store, _publish
            store = _get_store()
            active_tid = store.get_pref("activeThreadId")
            if not active_tid:
                # No chat conversation started yet -- silently drop.
                return
            # Idempotency: if we already appended this order_id's fill
            # (e.g. a retry / restart), skip. Cheap check on the most
            # recent N messages.
            recent = store.get_messages(active_tid)[-10:]
            for m in recent:
                content = m.get("content")
                if isinstance(content, str) and f"order #{order_id}" in content:
                    return
            if store.append_message(active_tid, role="user", content=msg):
                await _publish(
                    "thread_updated", client_id=None, thread_id=active_tid,
                )
        except Exception as e:
            self.logger.warning(f"chat fill append failed: {e}")

    def _on_order_status_for_strategies(self, trade) -> None:
        """Order status changes (Cancelled, Filled, etc). Forward to the same
        per-strategy tick trigger as fills — many status transitions also
        warrant re-planning.
        """
        # Only react to terminal statuses to avoid tick storms.
        status = getattr(trade.orderStatus, "status", "")
        if status not in ("Filled", "Cancelled", "ApiCancelled"):
            return
        # Reuse the same lookup as fill events; same outcome (schedule a tick).
        self._on_exec_details_for_strategies(trade, None)

    # === Layer 5: Pivot-loop engine ====================================

    async def start_pivot_loop_task(self, symbol: str) -> Dict[str, Any]:
        """Spawn the autonomous tick task for ``symbol``. Idempotent --
        if a task is already running for this symbol it's a no-op."""
        symbol = symbol.upper().strip()
        existing = self._pivot_tasks.get(symbol)
        if existing and not existing.done():
            return {"status": "already_running", "symbol": symbol}
        self._pivot_tasks[symbol] = asyncio.create_task(
            self._pivot_outer_loop(symbol)
        )
        self.logger.info(f"pivot-loop engine: started task for {symbol}")
        return {"status": "started", "symbol": symbol}

    async def stop_pivot_loop_task(self, symbol: str) -> Dict[str, Any]:
        """Cancel the tick task. Does NOT close any open position --
        caller is responsible for ordering the close first (the dashboard
        Stop Loop button + the chat agent's stop_pivot_loop flow handle
        that)."""
        symbol = symbol.upper().strip()
        task = self._pivot_tasks.pop(symbol, None)
        if task and not task.done():
            task.cancel()
            self.logger.info(f"pivot-loop engine: cancelled task for {symbol}")
            return {"status": "cancelled", "symbol": symbol}
        return {"status": "not_running", "symbol": symbol}

    async def _pivot_outer_loop(self, symbol: str) -> None:
        """Outer task: tick → sleep → tick. Tick interval adapts to RTH
        (60s) vs OTH (300s) so we don't burn CPU outside hours."""
        from . import pivot_loop as engine_mod
        try:
            while True:
                try:
                    await self._pivot_tick(symbol)
                except Exception:
                    self.logger.exception(
                        f"pivot-loop engine: tick failed for {symbol}"
                    )
                # Re-read the interval each iteration so we transition
                # cadence cleanly at 9:30 ET / 16:00 ET.
                await asyncio.sleep(engine_mod.current_tick_interval())
        except asyncio.CancelledError:
            self.logger.info(f"pivot-loop engine: outer loop for {symbol} cancelled")
            raise

    async def _pivot_tick(self, symbol: str) -> Dict[str, Any]:
        """One tick: read state, decide, execute, record. Returns a
        small dict describing what happened (for observability + tests).
        Defensive against every external failure -- a bad IBKR call OR
        a missing yfinance dep OR a SQLite hiccup just yields a logged
        warning and a "skipped" result, never a tick-loop crash."""
        # Lazy imports to avoid circular imports (pivot.py / catalysts.py
        # import nothing from client; routes.py provides _get_store).
        from .chat.routes import _get_store
        from . import pivot as pivot_mod
        from . import catalysts as cat_mod
        from . import pivot_loop as engine_mod
        try:
            from .notify import send as _ntfy_send
            def notify_warning(title, msg):
                _ntfy_send(title, msg, priority=4, tags=["warning"])
            def notify_info(title, msg):
                _ntfy_send(title, msg, priority=3, tags=["chart_with_upwards_trend"])
        except Exception:
            notify_warning = notify_info = lambda *_a, **_k: None  # noqa: E731

        store = _get_store()
        loop = store.get_pivot_loop(symbol)
        if loop is None or loop["status"] == "stopped":
            # Loop is gone -- cancel ourselves (idempotent if already cancelled).
            asyncio.create_task(self.stop_pivot_loop_task(symbol))
            return {"action": "self_cancelled", "reason": "loop missing or stopped"}

        # Live-mode circuit breaker (cross-symbol, account-wide). If
        # today's realized P&L blew through the daily loss limit, NO
        # autonomous entries fire regardless of per-loop math. Checked
        # BEFORE bars/catalyst fetches so we don't burn API quota when
        # the breaker has already tripped.
        try:
            from . import live_safety
            from .config import settings as _s
            if live_safety.is_live_mode():
                try:
                    raw = await self.get_account_summary()
                    summ = (raw or {}).get("summary") or {}
                    realized = float(summ.get("RealizedPnL") or 0)
                except Exception:
                    realized = 0.0
                if live_safety.check_daily_pnl_breaker(realized):
                    # Auto-pause this loop -- write to SQLite so the
                    # dashboard + chat reflect the state. The breaker
                    # itself stays tripped until tomorrow OR manual reset.
                    store.update_pivot_loop(symbol, status="paused")
                    asyncio.create_task(self.stop_pivot_loop_task(symbol))
                    return {
                        "action": "live_breaker_pause",
                        "reason": (
                            f"daily P&L ${realized:.2f} ≤ "
                            f"${_s.live_daily_loss_limit:.2f}"
                        ),
                    }
        except Exception as e:
            self.logger.debug(f"pivot-loop {symbol}: breaker check skipped: {e}")

        # Fetch bars + catalysts for the analysis.
        try:
            bars = await self.get_historical_bars(
                symbol, lookback_days=loop["lookback_days"] + 5
            )
        except Exception as e:
            self.logger.warning(f"pivot-loop {symbol}: bars fetch failed: {e}")
            return {"action": "skipped", "reason": f"bars: {e}"}
        if len(bars) > loop["lookback_days"]:
            bars = bars.tail(loop["lookback_days"]).reset_index(drop=True)
        catalysts = cat_mod.get_upcoming_catalysts(
            symbol, horizon_days=max(loop["lookback_days"], 30)
        )
        # Phase E: broader-market regime gate (SPY trend/ADX). Cached
        # 1h so we don't refetch SPY's 250d bars on every 60s tick.
        market_regime_enabled = await engine_mod.get_market_regime_enabled(self)
        # Phase F: news sentiment. Cached 6h per symbol; cheap on cache hit.
        try:
            from . import news_sentiment as news_mod
            news = await news_mod.get_news_sentiment(symbol)
        except Exception as e:
            self.logger.debug(f"pivot-loop {symbol}: news fetch failed: {e}")
            news = None
        # Per-loop tunables (Phase 6). Fall back to the function-level
        # defaults when None -- the loop row may have these unset for
        # symbols the operator hasn't customized.
        kwargs_overrides = {}
        if loop.get("min_volume_ratio") is not None:
            kwargs_overrides["min_volume_ratio"] = float(loop["min_volume_ratio"])
        if loop.get("max_vol_ratio") is not None:
            kwargs_overrides["max_vol_ratio"] = float(loop["max_vol_ratio"])
        # news threshold is applied by news_sentiment.evaluate_sentiment;
        # if the operator set a custom threshold, re-evaluate here.
        if (news is not None
                and loop.get("news_block_threshold") is not None):
            from . import news_sentiment as news_mod
            news = dict(news)  # shallow copy so we don't mutate cache
            news["sentiment_ok"] = news_mod.evaluate_sentiment(
                news.get("score"),
                block_threshold=int(loop["news_block_threshold"]),
            )
        try:
            analysis = pivot_mod.analyze_pivot_loop(
                bars, catalysts,
                catalyst_horizon_days=loop["catalyst_horizon_days"],
                market_regime_enabled=market_regime_enabled,
                news_sentiment=news,
                **kwargs_overrides,
            )
        except ValueError as e:
            self.logger.warning(f"pivot-loop {symbol}: analysis failed: {e}")
            return {"action": "skipped", "reason": f"analysis: {e}"}

        # Probe IBKR side-state needed by the decision policy.
        has_open_position = await self._symbol_has_open_position(symbol)
        cycles = store.get_pivot_loop_cycles(symbol, limit=3)
        recent_losses = sum(1 for c in cycles if c.get("win") == 0)

        decision = engine_mod.decide_next_action(
            loop, analysis,
            has_open_position=has_open_position,
            last_3_cycles_losses=recent_losses,
        )

        # Execute the decision.
        if decision.action == "no_op":
            # If the decision flagged a revert-to-waiting (entry IOC
            # didn't fill), unwind the entry_pending state.
            if (decision.extra or {}).get("revert_to_waiting"):
                store.update_pivot_loop(symbol, status="waiting")
            return {"action": "no_op", "reason": decision.reason}

        if decision.action == "auto_stop":
            self.logger.warning(
                f"pivot-loop {symbol}: AUTO-STOP -- {decision.reason}"
            )
            store.stop_pivot_loop(symbol)
            notify_warning(
                f"Pivot loop AUTO-STOPPED: {symbol}",
                decision.reason,
            )
            asyncio.create_task(self.stop_pivot_loop_task(symbol))
            return {"action": "auto_stop", "reason": decision.reason}

        if decision.action == "place_entry":
            return await self._pivot_place_entry(
                symbol, loop, decision, notify_info, store,
            )

        if decision.action == "place_oca":
            return await self._pivot_place_oca(
                symbol, loop, decision, notify_info, store,
            )

        if decision.action == "force_exit":
            return await self._pivot_force_exit(
                symbol, loop, analysis, notify_warning, store,
            )

        if decision.action == "record_cycle":
            return await self._pivot_record_cycle(
                symbol, loop, notify_info, store,
            )

        # monitor_entry / monitor_holding -- just observability.
        return {"action": decision.action, "reason": decision.reason}

    # ----- pivot-tick execution helpers --------------------------------

    async def _symbol_has_open_position(self, symbol: str) -> bool:
        """Quick check: do we hold any shares of `symbol` in the active
        account right now? Uses ib.portfolio() so we read live."""
        try:
            target = self.current_account
            for item in self.ib.portfolio():
                if (target and getattr(item, "account", None) != target):
                    continue
                if (getattr(item.contract, "symbol", "").upper() == symbol.upper()
                        and abs(float(getattr(item, "position", 0) or 0)) > 0):
                    return True
        except Exception as e:
            self.logger.debug(f"_symbol_has_open_position({symbol}): {e}")
        return False

    async def _pivot_place_entry(self, symbol, loop, decision, notify_info, store):
        """Place a BUY LMT IOC sized to current_capital."""
        extra = decision.extra or {}
        # Live ask for sizing.
        try:
            md = await self.get_market_data(symbol)
        except Exception as e:
            return {"action": "entry_failed", "reason": f"market data: {e}"}
        ask = float(md.get("ask") or md.get("last") or 0)
        if ask <= 0:
            return {"action": "entry_failed", "reason": "no live ask"}
        capital = float(loop["current_capital"])
        # Add a small price buffer above ask for IOC fill probability,
        # then size to integer shares.
        limit_price = round(ask * 1.003 + 0.10, 2)
        shares = max(1, int(capital / limit_price))
        result = await self.place_order(
            symbol=symbol, action="BUY", quantity=shares,
            order_type="LMT", limit_price=limit_price, tif="IOC",
            outside_rth=True, confirm=True,
        )
        status = (result or {}).get("status")
        if status in ("submitted", "transmitted", "filled", "needs_confirmation"):
            store.update_pivot_loop(
                symbol, status="entry_pending", current_shares=shares,
                entry_price=ask,
            )
            notify_info(
                f"Pivot loop entry submitted: {symbol}",
                f"{shares} sh @ ~${ask:.2f} (cap ${capital:.0f})",
            )
            return {"action": "entry_placed", "shares": shares, "ask": ask}
        return {"action": "entry_failed", "result": result}

    async def _pivot_place_oca(self, symbol, loop, decision, notify_info, store):
        """Entry filled -- attach OCA(target LMT GTC + stop STP)."""
        extra = decision.extra or {}
        target = float(extra.get("target_price") or loop.get("target_price") or 0)
        stop = float(extra.get("stop_price") or loop.get("stop_price") or 0)
        if target <= 0 or stop <= 0:
            return {"action": "oca_failed", "reason": "missing target/stop"}
        shares = int(loop.get("current_shares") or 0)
        if shares <= 0:
            # Re-derive from IBKR portfolio.
            for it in self.ib.portfolio():
                if (getattr(it.contract, "symbol", "").upper() == symbol.upper()
                        and abs(float(getattr(it, "position", 0) or 0)) > 0):
                    shares = abs(int(it.position))
                    break
        if shares <= 0:
            return {"action": "oca_failed", "reason": "no position to protect"}
        grp = f"pivot-{symbol.lower()}-{int(dt.datetime.now(dt.timezone.utc).timestamp())}"
        legs = [
            {"symbol": symbol, "action": "SELL", "quantity": shares,
             "order_type": "LMT", "limit_price": target, "tif": "GTC",
             "outside_rth": True},
            {"symbol": symbol, "action": "SELL", "quantity": shares,
             "order_type": "STP", "stop_price": stop, "tif": "GTC",
             "outside_rth": True},
        ]
        try:
            result = await self.place_oca_group(
                oca_group_name=grp, orders=legs, confirm=True,
            )
        except Exception as e:
            return {"action": "oca_failed", "reason": str(e)}

        # Look up the entry fill price now.
        entry_fill = None
        for it in self.ib.portfolio():
            if (getattr(it.contract, "symbol", "").upper() == symbol.upper()
                    and abs(float(getattr(it, "position", 0) or 0)) > 0):
                entry_fill = float(getattr(it, "averageCost", 0) or 0)
                break
        store.update_pivot_loop(
            symbol, status="holding", current_shares=shares,
            entry_fill_price=entry_fill,
        )
        notify_info(
            f"Pivot loop HOLDING: {symbol}",
            f"{shares} sh @ ${entry_fill:.2f}; OCA target ${target} / stop ${stop}",
        )
        return {"action": "oca_placed", "target": target, "stop": stop,
                "entry_fill": entry_fill}

    async def _pivot_force_exit(self, symbol, loop, analysis, notify_warning, store):
        """Catalyst-driven close: cancel OCA siblings, fast SELL LMT IOC."""
        # Cancel any open orders for this symbol first (the OCA pair).
        try:
            await self.cancel_all_orders(symbol=symbol, confirm=True)
        except Exception as e:
            self.logger.warning(f"pivot-loop {symbol}: pre-exit cancel failed: {e}")
        # Get bid for the LMT IOC.
        try:
            md = await self.get_market_data(symbol)
        except Exception as e:
            return {"action": "force_exit_failed", "reason": f"market data: {e}"}
        bid = float(md.get("bid") or md.get("last") or 0)
        if bid <= 0:
            return {"action": "force_exit_failed", "reason": "no live bid"}
        shares = int(loop.get("current_shares") or 0)
        if shares <= 0:
            return {"action": "force_exit_failed", "reason": "no shares to close"}
        limit_price = round(max(bid * 0.997 - 0.10, 0.01), 2)
        result = await self.place_order(
            symbol=symbol, action="SELL", quantity=shares,
            order_type="LMT", limit_price=limit_price, tif="DAY",
            outside_rth=True, confirm=True,
        )
        store.update_pivot_loop(symbol, status="exit_pending")
        notify_warning(
            f"Pivot loop catalyst exit: {symbol}",
            f"closing {shares} sh @ LMT ${limit_price} (catalyst in {analysis.days_to_next_catalyst}d)",
        )
        return {"action": "force_exit_placed", "result": result}

    async def _pivot_record_cycle(self, symbol, loop, notify_info, store):
        """Position has been closed externally (OCA child fired). Record
        the realized cycle by looking up the most recent SELL fill on
        this symbol from ib.trades()."""
        entry_fill = float(loop.get("entry_fill_price") or 0)
        shares = int(loop.get("current_shares") or 0)
        if entry_fill <= 0 or shares <= 0:
            # Nothing to compute -- just reset to waiting.
            store.update_pivot_loop(symbol, status="waiting", current_shares=0,
                                    entry_fill_price=None)
            return {"action": "cycle_skipped",
                    "reason": "no entry context to record"}

        # Find the most recent filled SELL trade for this symbol.
        exit_fill = None
        exit_reason = "target"
        try:
            trades = list(self.ib.trades())
        except Exception:
            trades = []
        for t in reversed(trades):
            try:
                if (getattr(t.contract, "symbol", "").upper() == symbol.upper()
                        and getattr(t.order, "action", "") == "SELL"
                        and getattr(t.orderStatus, "status", "") == "Filled"):
                    exit_fill = float(t.orderStatus.avgFillPrice)
                    # If it was a STP child, exit_reason = stop
                    if getattr(t.order, "orderType", "") == "STP":
                        exit_reason = "stop"
                    break
            except Exception:
                continue
        if exit_fill is None:
            exit_fill = entry_fill  # break-even fallback; conservative
            exit_reason = "manual"

        realized = (exit_fill - entry_fill) * shares
        capital_at_start = float(loop["current_capital"])
        updated = store.record_pivot_loop_cycle(
            symbol,
            capital_at_start=capital_at_start,
            entry_price=loop.get("entry_price"),
            entry_fill=entry_fill,
            entry_at=None,
            shares=shares,
            exit_fill=exit_fill,
            exit_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            exit_reason=exit_reason,
            realized_pnl=realized,
        )
        notify_info(
            f"Pivot cycle {updated.get('cycle_count', '?')} ({exit_reason.upper()}): {symbol}",
            f"P&L ${realized:+.2f}; capital now ${updated.get('current_capital', 0):.0f}",
        )
        return {"action": "cycle_recorded", "realized_pnl": realized,
                "exit_reason": exit_reason}

    # === end Pivot-loop engine =========================================

    async def resume_strategies_from_state(self) -> Dict[str, Any]:
        """On daemon startup, restart asyncio tasks for any active strategies
        that were running before the previous shutdown.
        """
        resumed: Dict[str, Any] = {"reversal": [], "swing": [], "pivot": []}

        # Reversal entries that were mid-execution
        for symbol, state in self._reversal_state_dict().items():
            if state.status in (
                ReversalStatus.WATCHING,
                ReversalStatus.PARTIALLY_FILLED,
                ReversalStatus.STALLED,
            ):
                if symbol in self._reversal_tasks and not self._reversal_tasks[symbol].done():
                    continue  # already running
                self._reversal_tasks[symbol] = asyncio.create_task(
                    self._reversal_loop(symbol, 3600)
                )
                resumed["reversal"].append(symbol)
                self.logger.info(
                    f"resumed reversal entry for {symbol} (status={state.status.value})"
                )

        # Swing strategies that were running
        for symbol, state in self._swing_state_dict().items():
            if state.state in (SwingState.HOLDING, SwingState.FLAT):
                if symbol in self._swing_tasks and not self._swing_tasks[symbol].done():
                    continue
                self._swing_tasks[symbol] = asyncio.create_task(
                    self._swing_loop(symbol, 3600)
                )
                resumed["swing"].append(symbol)
                self.logger.info(
                    f"resumed swing strategy for {symbol} (state={state.state.value})"
                )

        # Pivot loops that were running (Phase B). Read active loops
        # from chat.db's pivot_loops table and spawn a tick task per
        # symbol. Idempotent -- if a task already exists, skip.
        #
        # Live-mode auto-pause-on-first-connect: when we connect in live
        # mode AND the operator has live_auto_pause_loops_on_connect=True
        # (default), flip every still-active loop to 'paused' BEFORE
        # spawning tasks. Forces a manual unpause per symbol so nothing
        # trades automatically on a freshly-flipped account with
        # paper-tuned parameters.
        try:
            from .chat.routes import _get_store
            from . import live_safety
            store = _get_store()
            auto_pause = (
                live_safety.is_live_mode()
                and settings.live_auto_pause_loops_on_connect
            )
            for loop_row in store.list_pivot_loops(include_stopped=False):
                sym = loop_row["symbol"]
                if auto_pause and loop_row["status"] != "paused":
                    store.update_pivot_loop(sym, status="paused")
                    self.logger.warning(
                        f"live mode: auto-paused pivot loop for {sym} "
                        f"(was {loop_row['status']}); operator must "
                        f"manually resume"
                    )
                    try:
                        from .notify import send
                        send(
                            title=f"⏸ Pivot loop auto-paused: {sym}",
                            message=(
                                f"Daemon connected in LIVE mode -- {sym} "
                                f"loop paused (was {loop_row['status']}). "
                                f"Resume manually from the dashboard."
                            ),
                            priority=4,
                            tags=["pause_button"],
                        )
                    except Exception:
                        pass
                    continue  # don't spawn a tick task for paused loops
                existing = self._pivot_tasks.get(sym)
                if existing and not existing.done():
                    continue
                self._pivot_tasks[sym] = asyncio.create_task(
                    self._pivot_outer_loop(sym)
                )
                resumed["pivot"].append(sym)
                self.logger.info(
                    f"resumed pivot loop for {sym} (status={loop_row['status']})"
                )
        except Exception as e:
            self.logger.warning(f"could not resume pivot loops: {e}")

        return resumed

    async def reconcile_on_startup(self) -> Dict[str, Any]:
        """Compare persisted order IDs with IBKR's current open orders.

        On mismatch (stored ID not in IBKR's open list), prefer reality:
        clear the local reference so the next tick re-plans from a clean
        slate. Returns a summary of what was cleared.
        """
        if not self.is_connected() or not self.ib:
            return {"status": "skipped", "reason": "not_connected"}

        open_order_ids = {
            t.order.orderId
            for t in self.ib.trades()
            if t.orderStatus.status in ("PreSubmitted", "Submitted", "PendingSubmit")
        }
        cleared: Dict[str, list] = {"reversal": [], "swing": []}

        for symbol, state in self._reversal_state_dict().items():
            if state.protective_stop_order_id and state.protective_stop_order_id not in open_order_ids:
                self.logger.warning(
                    f"reversal {symbol}: stored protective stop {state.protective_stop_order_id} "
                    f"not in IBKR open orders; clearing local reference"
                )
                state.protective_stop_order_id = None
                cleared["reversal"].append(symbol)

        for symbol, state in self._swing_state_dict().items():
            entries_cleared: list[str] = []
            for field in ("protective_trail_order_id", "protective_stop_order_id", "dip_buy_order_id"):
                oid = getattr(state, field)
                if oid and oid not in open_order_ids:
                    self.logger.warning(
                        f"swing {symbol}: stored {field}={oid} not in IBKR open orders; clearing"
                    )
                    setattr(state, field, None)
                    entries_cleared.append(field)
            if "protective_trail_order_id" in entries_cleared or "protective_stop_order_id" in entries_cleared:
                state.oca_group = None
            if entries_cleared:
                cleared["swing"].append({"symbol": symbol, "fields": entries_cleared})

        self._persist_reversal_state()
        self._persist_swing_state()
        return {"status": "reconciled", "cleared": cleared}

    def _serialize_position(self, position) -> Dict:
        """Convert Position to serializable dict."""
        return {
            "symbol": position.contract.symbol,
            "secType": position.contract.secType,
            "exchange": position.contract.exchange,
            "position": safe_float(position.position),
            "avgCost": safe_float(position.avgCost),
            "marketPrice": safe_float(getattr(position, 'marketPrice', 0)),
            "marketValue": safe_float(getattr(position, 'marketValue', 0)),
            "unrealizedPNL": safe_float(getattr(position, 'unrealizedPNL', 0)),
            "realizedPNL": safe_float(getattr(position, 'realizedPNL', 0)),
            "account": position.account
        }
    
    def _serialize_account_value(self, account_value) -> Dict:
        """Convert AccountValue to serializable dict."""
        return {
            "tag": account_value.tag,
            "value": account_value.value,
            "currency": account_value.currency,
            "account": account_value.account
        }


# Global client instance
ibkr_client = IBKRClient()
