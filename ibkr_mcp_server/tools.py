"""MCP tools for IBKR functionality."""

import json
from typing import Any, Sequence

from mcp.server import Server
from mcp.types import Tool, TextContent, CallToolRequest

from .client import ibkr_client
from .utils import validate_symbols, IBKRError


# Create the server instance
server = Server("ibkr-mcp")


# Define all tools
TOOLS = [
    Tool(
        name="get_portfolio",
        description="Retrieve current portfolio positions and P&L from IBKR",
        inputSchema={
            "type": "object",
            "properties": {
                "account": {"type": "string", "description": "Account ID (optional, uses current account if not specified)"}
            },
            "additionalProperties": False
        }
    ),
    Tool(
        name="get_account_summary", 
        description="Get account balances and key metrics from IBKR",
        inputSchema={
            "type": "object",
            "properties": {
                "account": {"type": "string", "description": "Account ID (optional, uses current account if not specified)"}
            },
            "additionalProperties": False
        }
    ),
    Tool(
        name="switch_account",
        description="Switch between IBKR accounts",
        inputSchema={
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "description": "Account ID to switch to"}
            },
            "required": ["account_id"],
            "additionalProperties": False
        }
    ),
    Tool(
        name="get_accounts",
        description="Get available IBKR accounts and current account", 
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False}
    ),
    Tool(
        name="check_shortable_shares",
        description="Check short selling availability for securities",
        inputSchema={
            "type": "object",
            "properties": {
                "symbols": {"type": "string", "description": "Comma-separated list of symbols"},
                "account": {"type": "string", "description": "Account ID (optional, uses current account if not specified)"}
            },
            "required": ["symbols"],
            "additionalProperties": False
        }
    ),
    Tool(
        name="get_margin_requirements",
        description="Get margin requirements for securities",
        inputSchema={
            "type": "object",
            "properties": {
                "symbols": {"type": "string", "description": "Comma-separated list of symbols"},
                "account": {"type": "string", "description": "Account ID (optional, uses current account if not specified)"}
            },
            "required": ["symbols"],
            "additionalProperties": False
        }
    ),
    Tool(
        name="short_selling_analysis",
        description="Complete short selling analysis: availability, margin requirements, and summary",
        inputSchema={
            "type": "object",
            "properties": {
                "symbols": {"type": "string", "description": "Comma-separated list of symbols"},
                "account": {"type": "string", "description": "Account ID (optional, uses current account if not specified)"}
            },
            "required": ["symbols"],
            "additionalProperties": False
        }
    ),
    Tool(
        name="get_connection_status",
        description="Check IBKR TWS/Gateway connection status and account information",
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False}
    ),
    Tool(
        name="place_order",
        description=(
            "Place a single equity order. Supports MKT, LMT, STP, STP LMT, TRAIL, "
            "TRAIL LIMIT, LOO, MOO, LOC, MOC. Honors ENABLE_LIVE_TRADING and "
            "MAX_ORDER_SIZE. Set dry_run=true to validate without transmitting."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "action": {"type": "string", "enum": ["BUY", "SELL"]},
                "quantity": {"type": "integer", "minimum": 1},
                "order_type": {
                    "type": "string",
                    "enum": [
                        "MKT", "LMT", "STP", "STP LMT",
                        "TRAIL", "TRAIL LIMIT",
                        "LOO", "MOO", "LOC", "MOC",
                    ],
                },
                "limit_price": {"type": "number"},
                "stop_price": {"type": "number"},
                "trail_amount": {"type": "number"},
                "trail_percent": {"type": "number"},
                "trail_stop_price": {"type": "number"},
                "limit_price_offset": {"type": "number"},
                "tif": {"type": "string", "enum": ["DAY", "GTC", "IOC", "OPG"]},
                "outside_rth": {"type": "boolean"},
                "account": {"type": "string"},
                "dry_run": {"type": "boolean"},
            },
            "required": ["symbol", "action", "quantity", "order_type"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="check_regime",
        description=(
            "Evaluate the three-gate regime filter for a symbol. Gates: "
            "SMA(50) trend rising, ADX(14) below threshold, ATR%(14) below "
            "its 100-day average. Returns enabled/disabled plus the per-gate "
            "breakdown and a consecutive-days counter for whipsaw smoothing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "adx_threshold": {"type": "number"},
                "atr_lookback": {"type": "integer"},
                "sma_period": {"type": "integer"},
                "sma_lookback_days": {"type": "integer"},
                "require_all_gates": {"type": "boolean"},
                "smoothing_days": {"type": "integer"},
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="check_reversal_signals",
        description=(
            "Compute the 5 reversal signals for a symbol — bullish RSI divergence, "
            "RSI crossing above 30, MACD bullish crossover, higher swing-low, "
            "volume surge. Returns a signal count (0-5) and the recommended "
            "tranche to place (0 means hold). Stateless — does not trigger an entry."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "min_signals_for_entry": {"type": "integer", "default": 3},
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="start_reversal_entry",
        description=(
            "Start a tranched reversal entry plan for a symbol. Total capital "
            "is split across N tranches (default 3); each tranche fires when "
            "the signal count crosses the next threshold and has held for "
            "signal_window_days. Stop-and-wait kicks in if signals drop after "
            "tranche 1 fills. Runs an in-process hourly tick — Layer 5 will "
            "replace it with a real daemon."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "total_dollars": {"type": "number", "minimum": 1},
                "tranche_count": {"type": "integer", "minimum": 1, "default": 3},
                "tranche_sizing": {"type": "string", "enum": ["equal", "weighted"], "default": "equal"},
                "min_signals_for_entry": {"type": "integer", "default": 3},
                "signal_window_days": {"type": "integer", "default": 3},
                "stall_timeout_days": {"type": "integer", "default": 10},
                "protective_stop_atr_multiple": {"type": "number", "default": 2.0},
                "recheck_interval_seconds": {"type": "integer", "default": 3600},
            },
            "required": ["symbol", "total_dollars"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="stop_reversal_entry",
        description=(
            "Stop an active reversal entry. action='cancel' stops further "
            "tranches but leaves filled tranches alone. action='liquidate_filled' "
            "also market-sells anything filled. action='convert_to_swing_loop' "
            "returns not_implemented until Layer 4 ships."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": ["cancel", "liquidate_filled", "convert_to_swing_loop"],
                    "default": "cancel",
                },
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="get_reversal_status",
        description="Return the current state of an active or recent reversal entry.",
        inputSchema={
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="start_swing_strategy",
        description=(
            "Start the Layer 4 swing-trading loop on an existing position. "
            "Places an OCA protective pair (trailing SELL + hard STP at "
            "cost_basis - floor_offset), then on a fill re-enters via a LMT BUY "
            "at the dip price. Specify EXACTLY ONE of dip_amount or dip_percent."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "quantity": {"type": "integer", "minimum": 1},
                "cost_basis": {"type": "number", "minimum": 0.01},
                "trail_atr_multiplier": {"type": "number", "default": 2.0},
                "floor_offset": {"type": "number", "default": 0.0},
                "dip_amount": {"type": "number"},
                "dip_percent": {"type": "number"},
                "regime_filter_enabled": {"type": "boolean", "default": True},
                "require_close_confirmation": {"type": "boolean", "default": True},
                "require_volume_confirmation": {"type": "boolean", "default": False},
                "volume_threshold_multiplier": {"type": "number", "default": 1.0},
                "cooldown_hours": {"type": "integer", "default": 24},
                "recheck_interval_seconds": {"type": "integer", "default": 3600},
            },
            "required": ["symbol", "quantity", "cost_basis"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="stop_swing_strategy",
        description="Cancel all open swing orders and stop the loop for `symbol`.",
        inputSchema={
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="get_swing_status",
        description="Return the current state of a swing strategy (state machine, open orders, last fill).",
        inputSchema={
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="update_swing_params",
        description=(
            "Adjust an active swing strategy's tuning (trail multiplier, dip, "
            "floor, gates, etc.) without stopping the loop. Unknown params are ignored."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "trail_atr_multiplier": {"type": "number"},
                "floor_offset": {"type": "number"},
                "dip_amount": {"type": "number"},
                "dip_percent": {"type": "number"},
                "regime_filter_enabled": {"type": "boolean"},
                "require_volume_confirmation": {"type": "boolean"},
                "volume_threshold_multiplier": {"type": "number"},
                "cooldown_hours": {"type": "integer"},
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="place_oca_group",
        description=(
            "Place 2+ linked orders as a One-Cancels-All group. Filling any one "
            "cancels the others. Each order uses the same schema as place_order."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "oca_group_name": {"type": "string"},
                "oca_type": {"type": "integer", "enum": [1], "default": 1},
                "orders": {
                    "type": "array",
                    "minItems": 2,
                    "items": {"type": "object"},
                },
            },
            "required": ["oca_group_name", "orders"],
            "additionalProperties": False,
        },
    ),
]


# Register tools list handler
@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return TOOLS


# Register tool call handler  
@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> Sequence[TextContent]:
    """Handle tool calls."""
    try:
        if name == "get_portfolio":
            account = arguments.get("account")
            positions = await ibkr_client.get_portfolio(account)
            return [TextContent(
                type="text",
                text=json.dumps(positions, indent=2)
            )]
            
        elif name == "get_account_summary":
            account = arguments.get("account")
            summary = await ibkr_client.get_account_summary(account)
            return [TextContent(
                type="text", 
                text=json.dumps(summary, indent=2)
            )]
            
        elif name == "switch_account":
            account_id = arguments["account_id"]
            result = await ibkr_client.switch_account(account_id)
            return [TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]
            
        elif name == "get_accounts":
            accounts = await ibkr_client.get_accounts()
            return [TextContent(
                type="text",
                text=json.dumps(accounts, indent=2)
            )]
            
        elif name == "check_shortable_shares":
            symbols = arguments["symbols"]
            account = arguments.get("account")
            try:
                symbol_list = validate_symbols(symbols)
                results = []
                for symbol in symbol_list:
                    shortable_info = await ibkr_client.get_shortable_shares(symbol, account)
                    results.append({
                        "symbol": symbol,
                        "shortable_shares": shortable_info
                    })
                return [TextContent(
                    type="text",
                    text=json.dumps(results, indent=2)
                )]
            except Exception as e:
                return [TextContent(
                    type="text",
                    text=f"Error checking shortable shares: {str(e)}"
                )]
                
        elif name == "get_margin_requirements":
            symbols = arguments["symbols"]
            account = arguments.get("account")
            try:
                symbol_list = validate_symbols(symbols)
                results = []
                for symbol in symbol_list:
                    margin_info = await ibkr_client.get_margin_requirements(symbol, account)
                    results.append({
                        "symbol": symbol,
                        "margin_requirements": margin_info
                    })
                return [TextContent(
                    type="text",
                    text=json.dumps(results, indent=2)
                )]
            except Exception as e:
                return [TextContent(
                    type="text",
                    text=f"Error getting margin requirements: {str(e)}"
                )]
                
        elif name == "short_selling_analysis":
            symbols = arguments["symbols"]
            account = arguments.get("account")
            try:
                symbol_list = validate_symbols(symbols)
                analysis = await ibkr_client.short_selling_analysis(symbol_list, account)
                return [TextContent(
                    type="text",
                    text=json.dumps(analysis, indent=2)
                )]
            except Exception as e:
                return [TextContent(
                    type="text",
                    text=f"Error performing short selling analysis: {str(e)}"
                )]
                
        elif name == "place_order":
            result = await ibkr_client.place_order(**arguments)
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

        elif name == "check_regime":
            symbol = arguments.pop("symbol")
            result = await ibkr_client.check_regime(symbol, **arguments)
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

        elif name == "check_reversal_signals":
            symbol = arguments.pop("symbol")
            result = await ibkr_client.check_reversal_signals(symbol, **arguments)
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

        elif name == "start_reversal_entry":
            symbol = arguments.pop("symbol")
            total_dollars = arguments.pop("total_dollars")
            result = await ibkr_client.start_reversal_entry(symbol, total_dollars, **arguments)
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

        elif name == "stop_reversal_entry":
            symbol = arguments.pop("symbol")
            action = arguments.get("action", "cancel")
            result = await ibkr_client.stop_reversal_entry(symbol, action)
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

        elif name == "get_reversal_status":
            result = await ibkr_client.get_reversal_status(arguments["symbol"])
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

        elif name == "start_swing_strategy":
            symbol = arguments.pop("symbol")
            quantity = arguments.pop("quantity")
            cost_basis = arguments.pop("cost_basis")
            result = await ibkr_client.start_swing_strategy(
                symbol, quantity, cost_basis, **arguments
            )
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

        elif name == "stop_swing_strategy":
            result = await ibkr_client.stop_swing_strategy(arguments["symbol"])
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

        elif name == "get_swing_status":
            result = await ibkr_client.get_swing_status(arguments["symbol"])
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

        elif name == "update_swing_params":
            symbol = arguments.pop("symbol")
            result = await ibkr_client.update_swing_params(symbol, **arguments)
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

        elif name == "place_oca_group":
            result = await ibkr_client.place_oca_group(
                orders=arguments["orders"],
                oca_group_name=arguments["oca_group_name"],
                oca_type=arguments.get("oca_type", 1),
            )
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

        elif name == "get_connection_status":
            status = {
                "connected": ibkr_client.is_connected(),
                "host": ibkr_client.host,
                "port": ibkr_client.port,
                "client_id": ibkr_client.client_id,
                "current_account": ibkr_client.current_account,
                "available_accounts": ibkr_client.accounts,
                "paper_trading": ibkr_client.is_paper
            }
            return [TextContent(
                type="text",
                text=json.dumps(status, indent=2)
            )]
        
        else:
            return [TextContent(
                type="text",
                text=f"Unknown tool: {name}"
            )]
            
    except Exception as e:
        return [TextContent(
            type="text",
            text=f"Error executing tool {name}: {str(e)}"
        )]
