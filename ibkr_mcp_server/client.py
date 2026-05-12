"""IBKR Client with advanced trading capabilities."""

import asyncio
import datetime as dt
import logging
from dataclasses import asdict
from typing import Any, Dict, List, Optional, Union
from decimal import Decimal

from ib_async import IB, Stock, util
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
        asyncio.create_task(self._reconnect())
    
    def _on_error(self, reqId, errorCode, errorString, contract):
        """Centralized error logging."""
        # Don't log certain routine messages as errors
        if errorCode in [2104, 2106, 2158]:  # Market data warnings
            self.logger.debug(f"IBKR Info {errorCode}: {errorString}")
        else:
            self.logger.error(f"IBKR Error {errorCode}: {errorString} (reqId: {reqId})")
    
    async def _reconnect(self):
        """Background reconnection task."""
        try:
            await asyncio.sleep(self.reconnect_delay)
            await self.connect()
        except Exception as e:
            self.logger.error(f"Reconnection failed: {e}")
    
    def is_connected(self) -> bool:
        """Check connection status."""
        return self._connected and self.ib is not None and self.ib.isConnected()
    
    @rate_limit(calls_per_second=1.0)
    async def get_portfolio(self, account: Optional[str] = None) -> List[Dict]:
        """Get portfolio positions."""
        try:
            if not await self._ensure_connected():
                raise ConnectionError("Not connected to IBKR")
            
            account = account or self.current_account
            
            positions = await self.ib.reqPositionsAsync()
            
            portfolio = []
            for pos in positions:
                if not account or pos.account == account:
                    portfolio.append(self._serialize_position(pos))
            
            return portfolio
            
        except Exception as e:
            self.logger.error(f"Portfolio request failed: {e}")
            raise RuntimeError(f"IBKR API error: {str(e)}")
    
    @rate_limit(calls_per_second=1.0)
    async def get_account_summary(self, account: Optional[str] = None) -> List[Dict]:
        """Get account summary."""
        try:
            if not await self._ensure_connected():
                raise ConnectionError("Not connected to IBKR")
            
            account = account or self.current_account or "All"
            
            summary_tags = [
                'TotalCashValue', 'NetLiquidation', 'UnrealizedPnL', 'RealizedPnL',
                'GrossPositionValue', 'BuyingPower', 'EquityWithLoanValue',
                'PreviousDayEquityWithLoanValue', 'FullInitMarginReq', 'FullMaintMarginReq'
            ]
            
            account_values = await self.ib.reqAccountSummaryAsync(account, ','.join(summary_tags))
            
            return [self._serialize_account_value(av) for av in account_values]
            
        except Exception as e:
            self.logger.error(f"Account summary request failed: {e}")
            raise RuntimeError(f"IBKR API error: {str(e)}")
    
    @rate_limit(calls_per_second=0.5)
    async def get_shortable_shares(self, symbol: str, account: str = None) -> Dict:
        """Get short selling information for a symbol."""
        try:
            if not await self._ensure_connected():
                raise ConnectionError("Not connected to IBKR")
            
            contract = Stock(symbol, 'SMART', 'USD')
            
            # Qualify the contract
            qualified_contracts = await self.ib.reqContractDetailsAsync(contract)
            if not qualified_contracts:
                return {"error": "Contract not found"}
            
            qualified_contract = qualified_contracts[0].contract
            
            # Request shortable shares
            shortable_shares = await self.ib.reqShortableSharesAsync(qualified_contract)
            
            # Get current market data
            ticker = self.ib.reqMktData(qualified_contract, '', False, False)
            await asyncio.sleep(1.5)  # Wait for market data
            
            result = {
                "symbol": symbol,
                "shortable_shares": shortable_shares if shortable_shares != -1 else "Unlimited",
                "current_price": safe_float(ticker.last or ticker.close),
                "bid": safe_float(ticker.bid),
                "ask": safe_float(ticker.ask),
                "contract_id": qualified_contract.conId
            }
            
            # Clean up ticker
            self.ib.cancelMktData(qualified_contract)
            
            return result
            
        except Exception as e:
            self.logger.error(f"Error getting shortable shares for {symbol}: {e}")
            return {"error": str(e)}

    @retry_on_failure(max_attempts=2)
    async def get_margin_requirements(self, symbol: str, account: str = None) -> Dict:
        """Get margin requirements for a symbol."""
        try:
            if not await self._ensure_connected():
                raise ConnectionError("Not connected to IBKR")
                
            # Create contract
            contract = Stock(symbol, 'SMART', 'USD')
            await self.ib.qualifyContractsAsync([contract])
            
            if not contract.conId:
                return {"error": f"Invalid symbol: {symbol}"}
            
            # Get margin requirements - simplified for now
            # Note: IBKR API doesn't provide direct margin requirements
            # This would typically require additional market data subscriptions
            margin_info = {
                "symbol": symbol,
                "contract_id": contract.conId,
                "exchange": contract.exchange,
                "margin_requirement": "Market data subscription required",
                "note": "Use TWS for detailed margin calculations"
            }
            
            return margin_info
            
        except Exception as e:
            self.logger.error(f"Error getting margin info for {symbol}: {e}")
            return {"error": str(e)}

    async def short_selling_analysis(self, symbols: List[str], account: str = None) -> Dict:
        """Complete short selling analysis for multiple symbols."""
        try:
            if not await self._ensure_connected():
                raise ConnectionError("Not connected to IBKR")
            
            analysis = {
                "account": account or self.current_account,
                "symbols_analyzed": symbols,
                "shortable_data": {},
                "margin_data": {},
                "summary": {
                    "total_symbols": len(symbols),
                    "shortable_count": 0,
                    "errors": []
                }
            }
            
            # Get shortable shares data
            for symbol in symbols:
                try:
                    shortable_info = await self.get_shortable_shares(symbol, account)
                    analysis["shortable_data"][symbol] = shortable_info
                    
                    if "error" not in shortable_info:
                        analysis["summary"]["shortable_count"] += 1
                except Exception as e:
                    analysis["summary"]["errors"].append(f"{symbol}: {str(e)}")
            
            # Get margin requirements
            for symbol in symbols:
                try:
                    margin_info = await self.get_margin_requirements(symbol, account)
                    analysis["margin_data"][symbol] = margin_info
                except Exception as e:
                    analysis["summary"]["errors"].append(f"{symbol} margin: {str(e)}")
            
            return analysis
            
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
        await self.ib.qualifyContractsAsync(contract)
        if not contract.conId:
            raise RuntimeError(f"Could not qualify contract for {symbol}")

        bars = await self.ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr=f"{lookback_days} D",
            barSizeSetting=bar_size,
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )
        df = util.df(bars)
        if df is None or len(df) == 0:
            raise RuntimeError(f"No historical bars returned for {symbol}")
        return df

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

    async def stop_reversal_entry(self, symbol: str, action: str = "cancel") -> Dict:
        """Stop a running reversal entry.

        `action`:
          - "cancel": stop the tick, leave filled tranches alone
          - "liquidate_filled": stop the tick AND market-sell everything filled
          - "convert_to_swing_loop": NOT YET IMPLEMENTED (Layer 4)
        """
        states = self._reversal_state_dict()
        state = states.get(symbol)
        if not state:
            return {"status": "error", "message": f"no reversal entry for {symbol}"}

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

    async def stop_swing_strategy(self, symbol: str) -> Dict:
        """Cancel any open swing orders and stop the loop."""
        states = self._swing_state_dict()
        state = states.get(symbol)
        if not state:
            return {"status": "error", "message": f"no swing strategy for {symbol}"}

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
        state = self._swing_state_dict().get(symbol)
        if not state:
            return {"status": "not_found", "symbol": symbol}
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
        }

    async def update_swing_params(self, symbol: str, **params: Any) -> Dict:
        state = self._swing_state_dict().get(symbol)
        if not state:
            return {"status": "error", "message": f"no swing strategy for {symbol}"}
        valid = {f for f in SwingConfig.__dataclass_fields__}
        applied = {}
        for k, v in params.items():
            if k in valid:
                setattr(state.config, k, v)
                applied[k] = v
        self._persist_swing_state()
        return {"status": "updated", "symbol": symbol, "applied": applied}

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
        states = self._reversal_state_dict()
        state = states.get(symbol)
        if not state:
            return {"status": "not_found", "symbol": symbol}
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
        """
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

        if not await self._ensure_connected():
            return self._order_response(
                "error",
                req=req,
                preview=preview,
                message="Not connected to IBKR",
            )

        try:
            contract = Stock(req.symbol, "SMART", "USD")
            qualified = await self.ib.qualifyContractsAsync(contract)
            if not qualified or not contract.conId:
                return self._order_response(
                    "error",
                    req=req,
                    preview=preview,
                    message=f"Could not qualify contract for {req.symbol}",
                )

            ib_order = build_order(req)
            trade = self.ib.placeOrder(contract, ib_order)
            return self._order_response(
                "submitted",
                req=req,
                preview=preview,
                order_id=getattr(trade.order, "orderId", None),
                perm_id=getattr(trade.order, "permId", None) or None,
                message="Order submitted",
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
    ) -> Dict:
        """Place 2+ linked orders that cancel each other on any fill."""
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

        if any(r.dry_run for r in prepared):
            return {
                "status": "dry_run",
                "group_id": oca_group_name,
                "orders": [{"preview": p, "order_id": None, "perm_id": None} for p in previews],
                "message": "dry_run=true on one or more legs; OCA group not transmitted",
            }

        if not await self._ensure_connected():
            return {
                "status": "error",
                "group_id": oca_group_name,
                "orders": [{"preview": p, "order_id": None, "perm_id": None} for p in previews],
                "message": "Not connected to IBKR",
            }

        results: List[Dict] = []
        try:
            for req, preview in zip(prepared, previews):
                contract = Stock(req.symbol, "SMART", "USD")
                qualified = await self.ib.qualifyContractsAsync(contract)
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
