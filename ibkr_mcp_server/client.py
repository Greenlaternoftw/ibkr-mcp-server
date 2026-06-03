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

    async def resume_strategies_from_state(self) -> Dict[str, Any]:
        """On daemon startup, restart asyncio tasks for any active strategies
        that were running before the previous shutdown.
        """
        resumed: Dict[str, Any] = {"reversal": [], "swing": []}

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
