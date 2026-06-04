"""MCP tools for IBKR functionality."""

import json
from typing import Any, Sequence

from mcp.server import Server
from mcp.types import Tool, TextContent, ImageContent, CallToolRequest

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
                "confirm": {
                    "type": "boolean",
                    "default": False,
                    "description": "Required when REQUIRE_CONFIRMATION_FOR_DESTRUCTIVE_TOOLS is on; otherwise ignored.",
                },
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
                "confirm": {
                    "type": "boolean",
                    "default": False,
                    "description": "Required when REQUIRE_CONFIRMATION_FOR_DESTRUCTIVE_TOOLS is on.",
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
            "properties": {
                "symbol": {"type": "string"},
                "confirm": {
                    "type": "boolean",
                    "default": False,
                    "description": "Required when REQUIRE_CONFIRMATION_FOR_DESTRUCTIVE_TOOLS is on.",
                },
            },
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
        name="record_portfolio_snapshot_now",
        description=(
            "Force a portfolio equity snapshot RIGHT NOW. Normally the "
            "daemon records one every hour via a background task; this "
            "tool is for the operator to seed the equity-curve chart "
            "with extra data points (e.g. after a manual restart, or "
            "immediately after a deploy when the table is empty). "
            "Read-only side-effect: just records a row in chat.db's "
            "portfolio_snapshots table; doesn't touch the broker. Safe "
            "to call any time."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="get_portfolio_equity_curve",
        description=(
            "Render the account's equity curve from accumulated daily/hourly "
            "snapshots. Shows NetLiquidation over time as a single line, "
            "title carries total % change. Default lookback is 30 days. "
            "Requires at least 2 snapshots in the window -- the background "
            "snapshot task records one every hour by default, so the chart "
            "becomes useful after a few hours of daemon uptime. Use when "
            "the user asks 'how am I doing this month', 'show my P&L', "
            "'portfolio over time', etc."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "account": {
                    "type": "string",
                    "description": "Account ID. Defaults to current account.",
                },
                "lookback_days": {
                    "type": "integer",
                    "description": "Calendar days of snapshot history to plot. Default 30.",
                    "default": 30,
                    "minimum": 1,
                    "maximum": 365,
                },
                "theme": {
                    "type": "string",
                    "enum": ["dark", "light"],
                    "default": "dark",
                },
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="get_regime_chart",
        description=(
            "Render a price chart with the regime filter's verdict overlaid. "
            "Title shows ENABLED / DISABLED + which gates failed (trend, "
            "trend strength, volatility). Text result includes the gate "
            "numbers (ADX, ATR%, SMA slope). Use when the user asks why "
            "the regime is on/off, why no trades are firing, or wants to "
            "see the regime's reasoning visually."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "lookback_days": {
                    "type": "integer",
                    "description": "Calendar days of history. Default 250 (regime needs ADX/ATR warmup).",
                    "default": 250,
                    "minimum": 60,
                    "maximum": 730,
                },
                "theme": {
                    "type": "string",
                    "enum": ["dark", "light"],
                    "default": "dark",
                },
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="get_reversal_visualization",
        description=(
            "Render a price chart with the reversal entry's tranche fills "
            "overlaid: one marker per filled tranche (labeled with index "
            "and fill price), an average-fill horizontal line, and a text "
            "summary of remaining budget + unrealized P&L. Use when the "
            "user asks about a reversal entry in progress -- 'how's my "
            "TSLA reversal', 'what tranches have filled', etc. Errors if "
            "no active reversal exists for the symbol."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "lookback_days": {
                    "type": "integer",
                    "description": "Calendar days of history. Default 180.",
                    "default": 180,
                    "minimum": 5,
                    "maximum": 730,
                },
                "theme": {
                    "type": "string",
                    "enum": ["dark", "light"],
                    "default": "dark",
                },
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="get_swing_visualization",
        description=(
            "Render the swing strategy's state on top of a candlestick chart. "
            "Shows price history with the operator's cost basis, the hard "
            "floor (STP fires here), the current trail-stop estimate (moves "
            "with price), the dip-buy target if waiting on a re-entry, and "
            "a marker at the last fill. Use this when the user asks about "
            "'my AAPL swing', 'how's my F position', or wants to see how "
            "their strategy is performing visually. Requires an active swing "
            "strategy for the symbol -- if there isn't one, the result "
            "will say so and you should suggest get_chart for a generic view."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Symbol with an active swing strategy."},
                "lookback_days": {
                    "type": "integer",
                    "description": "Calendar days of history. Default 180.",
                    "default": 180,
                    "minimum": 5,
                    "maximum": 730,
                },
                "theme": {
                    "type": "string",
                    "enum": ["dark", "light"],
                    "default": "dark",
                },
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="get_chart",
        description=(
            "Fetch historical price bars for a symbol and render a candlestick "
            "chart with moving averages, returned as both a PNG image (shown "
            "inline in the chat UI) and a short numeric summary. Use this "
            "whenever the user asks 'show me X', 'chart X', or wants to see "
            "what a stock has been doing visually. Defaults to ~180 calendar "
            "days of daily bars with SMA20 + SMA50 overlays. For a chart with "
            "the user's strategy overlaid (cost basis, trail stop, etc.), "
            "use get_swing_visualization instead when a swing strategy is active."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Ticker symbol, e.g. AAPL"},
                "lookback_days": {
                    "type": "integer",
                    "description": "Calendar days of history. Default 180.",
                    "default": 180,
                    "minimum": 5,
                    "maximum": 730,
                },
                "theme": {
                    "type": "string",
                    "enum": ["dark", "light"],
                    "default": "dark",
                    "description": "Chart theme. Default dark matches the chat UI.",
                },
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="tick_now",
        description=(
            "Force an immediate tick on an active swing or reversal strategy. "
            "Useful for testing, after a manual config change, or to react to "
            "market news without waiting for the next scheduled tick (default "
            "1 hour). Returns the action the tick took."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "kind": {"type": "string", "enum": ["swing", "reversal"], "default": "swing"},
            },
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
                "confirm": {
                    "type": "boolean",
                    "default": False,
                    "description": "Required when REQUIRE_CONFIRMATION_FOR_DESTRUCTIVE_TOOLS is on AND a structural param is being changed.",
                },
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
                "dry_run": {
                    "type": "boolean",
                    "default": False,
                    "description": "Validate every leg and return a preview without transmission.",
                },
                "confirm": {
                    "type": "boolean",
                    "default": False,
                    "description": "Required when REQUIRE_CONFIRMATION_FOR_DESTRUCTIVE_TOOLS is on.",
                },
            },
            "required": ["oca_group_name", "orders"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="start_pivot_loop",
        description=(
            "Start a persistent pivot-loop strategy for a symbol. Loop "
            "state (capital, current position, cycle counters, compound "
            "flag) lives in SQLite -- survives restarts, cross-device, "
            "and across separate chat conversations. One loop per "
            "symbol; fails if a loop already exists (stop it first)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "initial_capital": {"type": "number", "minimum": 100},
                "lookback_days": {
                    "type": "integer", "minimum": 3, "maximum": 180,
                },
                "compound": {
                    "type": "boolean",
                    "default": True,
                    "description": "If true, each cycle's realized P&L is added to the next cycle's capital.",
                },
                "entry_price": {"type": "number"},
                "target_price": {"type": "number"},
                "stop_price": {"type": "number"},
                "catalyst_horizon_days": {
                    "type": "integer", "default": 2,
                    "description": "Refuse new entries within this many days of an earnings/ex-div event.",
                },
                "max_drawdown_pct": {
                    "type": "number", "default": 50.0,
                    "description": "Auto-stop the loop when cumulative loss exceeds this % of initial capital.",
                },
                "min_volume_ratio": {
                    "type": "number",
                    "description": "Per-loop override of the Phase C volume gate. recent_vol / lookback_vol must meet this. Default daemon-wide is 0.8.",
                },
                "max_vol_ratio": {
                    "type": "number",
                    "description": "Per-loop override of the Phase D realized-vol gate. recent_rvol / lookback_rvol must NOT exceed this. Default is 1.5.",
                },
                "news_block_threshold": {
                    "type": "integer",
                    "description": "Per-loop override of the Phase F news gate. Block entry when sentiment score is ≤ this value. Default is -5.",
                },
                "notes": {"type": "string"},
            },
            "required": ["symbol", "initial_capital", "lookback_days"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="get_pivot_loop_status",
        description=(
            "Get the current state + cycle history for a pivot loop. "
            "Omit symbol to list all active loops (and stopped ones if "
            "include_stopped=true)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "include_stopped": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="update_pivot_loop_state",
        description=(
            "Update mutable fields of a pivot loop (status, "
            "current_capital, entry/target/stop prices, current_shares, "
            "entry_fill_price, notes). Use this to track entry/exit "
            "progress during a cycle. To record a COMPLETED cycle with "
            "P&L, use record_pivot_loop_cycle instead -- it atomically "
            "updates all the roll-up counters."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["waiting", "entry_pending", "holding",
                             "exit_pending", "paused"],
                },
                "current_capital": {"type": "number"},
                "entry_price": {"type": "number"},
                "target_price": {"type": "number"},
                "stop_price": {"type": "number"},
                "current_shares": {"type": "integer"},
                "entry_fill_price": {"type": "number"},
                "notes": {"type": "string"},
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="record_pivot_loop_cycle",
        description=(
            "Append a completed cycle (entry+exit pair) to a loop's audit "
            "trail AND atomically update the roll-up counters "
            "(cycle_count, win/loss, cumulative_realized, current_capital). "
            "Call this once per completed round-trip, AFTER the exit "
            "fills. Compounding (if enabled) is applied here."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "capital_at_start": {
                    "type": "number",
                    "description": "Capital deployed for this cycle (= current_capital at entry).",
                },
                "entry_price": {"type": "number"},
                "entry_fill": {"type": "number"},
                "entry_at": {
                    "type": "string",
                    "description": "ISO timestamp of the entry fill.",
                },
                "shares": {"type": "integer"},
                "exit_fill": {"type": "number"},
                "exit_at": {
                    "type": "string",
                    "description": "ISO timestamp of the exit fill.",
                },
                "exit_reason": {
                    "type": "string",
                    "enum": ["target", "stop", "catalyst", "manual", "drawdown"],
                },
                "realized_pnl": {
                    "type": "number",
                    "description": "Signed $: (exit_fill - entry_fill) × shares.",
                },
            },
            "required": ["symbol", "capital_at_start", "realized_pnl"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="stop_pivot_loop",
        description=(
            "Stop a pivot loop. The row + cycle history are preserved "
            "(status flips to 'stopped' rather than being deleted) so "
            "the final ledger remains queryable via get_pivot_loop_status. "
            "Caller is responsible for closing any open position FIRST "
            "via place_order before stopping."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="get_open_orders",
        description=(
            "List currently open orders (pending, partially filled, or "
            "awaiting cancellation) for the active IBKR account. Returns "
            "order_id, symbol, action, order_type, quantity, prices, "
            "status, parent_id, oca_group for each. Use before cancel_order "
            "or cancel_all_orders to know what's out there."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "account": {"type": "string"},
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="cancel_order",
        description=(
            "Cancel a single open order by its IB order_id. Honors the "
            "destructive-action confirmation gate: returns a preview when "
            "REQUIRE_CONFIRMATION_FOR_DESTRUCTIVE_TOOLS is on and confirm "
            "is not true."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "order_id": {"type": "integer"},
                "confirm": {
                    "type": "boolean",
                    "default": False,
                    "description": "Required when the destructive-tool gate is on.",
                },
            },
            "required": ["order_id"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="cancel_all_orders",
        description=(
            "Cancel every open order for the active IBKR account, optionally "
            "filtered to a specific symbol. Returns a preview unless "
            "confirm=true and the destructive-action gate is enabled. "
            "Used by the dashboard's 'Cancel All' button + the Take "
            "Profits flow (cancels attached child orders before the new "
            "SELL is placed)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Optional. Filter cancellations to one symbol; omit to cancel everything.",
                },
                "account": {"type": "string"},
                "confirm": {
                    "type": "boolean",
                    "default": False,
                    "description": "Required when the destructive-tool gate is on.",
                },
            },
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
async def call_tool(
    name: str, arguments: dict[str, Any]
) -> Sequence[TextContent | ImageContent]:
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
                    # get_shortable_shares already returns a dict shaped with
                    # symbol/shortable_shares/classification/... — pass it
                    # through directly instead of double-nesting under another
                    # "symbol"/"shortable_shares" wrapper.
                    shortable_info = await ibkr_client.get_shortable_shares(symbol, account)
                    results.append(shortable_info)
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
                    # Same as check_shortable_shares: pass the structured
                    # response from get_margin_requirements through directly.
                    margin_info = await ibkr_client.get_margin_requirements(symbol, account)
                    results.append(margin_info)
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
            confirm = arguments.get("confirm", False)
            result = await ibkr_client.stop_reversal_entry(symbol, action, confirm=confirm)
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
            confirm = arguments.get("confirm", False)
            result = await ibkr_client.stop_swing_strategy(arguments["symbol"], confirm=confirm)
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

        elif name == "get_swing_status":
            result = await ibkr_client.get_swing_status(arguments["symbol"])
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

        elif name == "tick_now":
            kind = arguments.get("kind", "swing")
            result = await ibkr_client.tick_now(arguments["symbol"], kind=kind)
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

        elif name == "get_chart":
            # Returns BOTH a text summary (Claude reads + can quote it)
            # AND an inline image block (rendered in the chat UI).
            result = await ibkr_client.get_chart(
                arguments["symbol"],
                lookback_days=arguments.get("lookback_days", 180),
                theme=arguments.get("theme", "dark"),
            )
            if result.get("status") != "ok":
                # Error path -- return text only so the model can surface it.
                return [TextContent(
                    type="text",
                    text=json.dumps(result, indent=2, default=str),
                )]
            png_b64 = result.pop("image_png_b64")
            return [
                TextContent(
                    type="text",
                    text=json.dumps(result, indent=2, default=str),
                ),
                ImageContent(
                    type="image",
                    data=png_b64,
                    mimeType="image/png",
                ),
            ]

        elif name == "record_portfolio_snapshot_now":
            # No image content -- this is a write tool. The model
            # gets back the snapshot fields so it can quote the
            # numbers ("recorded $X at ...") to the operator.
            result = await ibkr_client.record_portfolio_snapshot()
            return [TextContent(
                type="text",
                text=json.dumps(result, indent=2, default=str),
            )]

        elif name == "get_portfolio_equity_curve":
            result = await ibkr_client.get_portfolio_equity_curve(
                account=arguments.get("account"),
                lookback_days=arguments.get("lookback_days", 30),
                theme=arguments.get("theme", "dark"),
            )
            if result.get("status") != "ok":
                return [TextContent(
                    type="text",
                    text=json.dumps(result, indent=2, default=str),
                )]
            png_b64 = result.pop("image_png_b64")
            return [
                TextContent(
                    type="text",
                    text=json.dumps(result, indent=2, default=str),
                ),
                ImageContent(
                    type="image",
                    data=png_b64,
                    mimeType="image/png",
                ),
            ]

        elif name == "get_regime_chart":
            result = await ibkr_client.get_regime_chart(
                arguments["symbol"],
                lookback_days=arguments.get("lookback_days", 250),
                theme=arguments.get("theme", "dark"),
            )
            if result.get("status") != "ok":
                return [TextContent(
                    type="text",
                    text=json.dumps(result, indent=2, default=str),
                )]
            png_b64 = result.pop("image_png_b64")
            return [
                TextContent(
                    type="text",
                    text=json.dumps(result, indent=2, default=str),
                ),
                ImageContent(
                    type="image",
                    data=png_b64,
                    mimeType="image/png",
                ),
            ]

        elif name == "get_reversal_visualization":
            result = await ibkr_client.get_reversal_visualization(
                arguments["symbol"],
                lookback_days=arguments.get("lookback_days", 180),
                theme=arguments.get("theme", "dark"),
            )
            if result.get("status") != "ok":
                return [TextContent(
                    type="text",
                    text=json.dumps(result, indent=2, default=str),
                )]
            png_b64 = result.pop("image_png_b64")
            return [
                TextContent(
                    type="text",
                    text=json.dumps(result, indent=2, default=str),
                ),
                ImageContent(
                    type="image",
                    data=png_b64,
                    mimeType="image/png",
                ),
            ]

        elif name == "get_swing_visualization":
            # Same text+image shape as get_chart but with strategy overlays
            # baked into the PNG (cost basis, floor, trail stop, dip target,
            # last fill). Falls back to text-only on missing strategy / no
            # bars; the model can then point the user at get_chart.
            result = await ibkr_client.get_swing_visualization(
                arguments["symbol"],
                lookback_days=arguments.get("lookback_days", 180),
                theme=arguments.get("theme", "dark"),
            )
            if result.get("status") != "ok":
                return [TextContent(
                    type="text",
                    text=json.dumps(result, indent=2, default=str),
                )]
            png_b64 = result.pop("image_png_b64")
            return [
                TextContent(
                    type="text",
                    text=json.dumps(result, indent=2, default=str),
                ),
                ImageContent(
                    type="image",
                    data=png_b64,
                    mimeType="image/png",
                ),
            ]

        elif name == "update_swing_params":
            symbol = arguments.pop("symbol")
            result = await ibkr_client.update_swing_params(symbol, **arguments)
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

        elif name == "place_oca_group":
            result = await ibkr_client.place_oca_group(
                orders=arguments["orders"],
                oca_group_name=arguments["oca_group_name"],
                oca_type=arguments.get("oca_type", 1),
                dry_run=arguments.get("dry_run", False),
                confirm=arguments.get("confirm", False),
            )
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

        elif name == "start_pivot_loop":
            from .chat.routes import _get_store
            try:
                loop = _get_store().create_pivot_loop(
                    arguments["symbol"],
                    initial_capital=float(arguments["initial_capital"]),
                    lookback_days=int(arguments["lookback_days"]),
                    compound=bool(arguments.get("compound", True)),
                    entry_price=arguments.get("entry_price"),
                    target_price=arguments.get("target_price"),
                    stop_price=arguments.get("stop_price"),
                    catalyst_horizon_days=int(arguments.get("catalyst_horizon_days", 2)),
                    max_drawdown_pct=float(arguments.get("max_drawdown_pct", 50.0)),
                    min_volume_ratio=arguments.get("min_volume_ratio"),
                    max_vol_ratio=arguments.get("max_vol_ratio"),
                    news_block_threshold=arguments.get("news_block_threshold"),
                    notes=arguments.get("notes"),
                )
                # Spawn the autonomous tick task so the loop starts
                # ticking immediately. Idempotent.
                try:
                    await ibkr_client.start_pivot_loop_task(arguments["symbol"])
                except Exception as e:
                    loop["engine_warning"] = f"engine spawn failed: {e}"
                return [TextContent(
                    type="text", text=json.dumps(loop, indent=2, default=str),
                )]
            except Exception as e:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": str(e)}, indent=2),
                )]

        elif name == "get_pivot_loop_status":
            from .chat.routes import _get_store
            store = _get_store()
            sym = arguments.get("symbol")
            if sym:
                loop = store.get_pivot_loop(sym)
                if loop is None:
                    return [TextContent(
                        type="text",
                        text=json.dumps({"error": f"no loop for {sym}"}),
                    )]
                cycles = store.get_pivot_loop_cycles(sym, limit=50)
                return [TextContent(
                    type="text",
                    text=json.dumps({"loop": loop, "cycles": cycles}, indent=2, default=str),
                )]
            loops = store.list_pivot_loops(
                include_stopped=bool(arguments.get("include_stopped", False))
            )
            return [TextContent(
                type="text", text=json.dumps({"loops": loops}, indent=2, default=str),
            )]

        elif name == "update_pivot_loop_state":
            from .chat.routes import _get_store
            sym = arguments.pop("symbol")
            try:
                out = _get_store().update_pivot_loop(sym, **arguments)
            except ValueError as e:
                return [TextContent(
                    type="text", text=json.dumps({"error": str(e)}),
                )]
            if out is None:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": f"no loop for {sym}"}),
                )]
            return [TextContent(
                type="text", text=json.dumps(out, indent=2, default=str),
            )]

        elif name == "record_pivot_loop_cycle":
            from .chat.routes import _get_store
            try:
                loop = _get_store().record_pivot_loop_cycle(
                    arguments["symbol"],
                    capital_at_start=float(arguments["capital_at_start"]),
                    entry_price=arguments.get("entry_price"),
                    entry_fill=arguments.get("entry_fill"),
                    entry_at=arguments.get("entry_at"),
                    shares=arguments.get("shares"),
                    exit_fill=arguments.get("exit_fill"),
                    exit_at=arguments.get("exit_at"),
                    exit_reason=arguments.get("exit_reason"),
                    realized_pnl=float(arguments["realized_pnl"]),
                )
                return [TextContent(
                    type="text", text=json.dumps(loop, indent=2, default=str),
                )]
            except (KeyError, ValueError) as e:
                return [TextContent(
                    type="text", text=json.dumps({"error": str(e)}),
                )]

        elif name == "stop_pivot_loop":
            from .chat.routes import _get_store
            sym = arguments["symbol"]
            out = _get_store().stop_pivot_loop(sym)
            if out is None:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": f"no active loop for {sym}"}),
                )]
            # Cancel the autonomous tick task too -- otherwise it would
            # keep ticking against a 'stopped' row and immediately
            # self-cancel on the next tick anyway.
            try:
                await ibkr_client.stop_pivot_loop_task(sym)
            except Exception as e:
                out["engine_warning"] = f"task cancel failed: {e}"
            return [TextContent(
                type="text", text=json.dumps(out, indent=2, default=str),
            )]

        elif name == "get_open_orders":
            result = await ibkr_client.get_open_orders(arguments.get("account"))
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

        elif name == "cancel_order":
            result = await ibkr_client.cancel_order(
                order_id=arguments["order_id"],
                confirm=arguments.get("confirm", False),
            )
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

        elif name == "cancel_all_orders":
            result = await ibkr_client.cancel_all_orders(
                symbol=arguments.get("symbol"),
                account=arguments.get("account"),
                confirm=arguments.get("confirm", False),
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
