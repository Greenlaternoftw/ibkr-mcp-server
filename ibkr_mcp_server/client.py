"""IBKR Client with advanced trading capabilities."""

import asyncio
import logging
from typing import Dict, List, Optional, Union
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
