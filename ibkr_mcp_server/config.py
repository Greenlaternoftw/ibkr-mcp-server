"""Configuration management for IBKR MCP Server."""

import os
from typing import List, Optional
from pydantic_settings import BaseSettings
from pydantic import field_validator


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # IBKR Connection
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 7497
    ibkr_client_id: int = 1
    ibkr_is_paper: bool = True
    
    # Account Management
    ibkr_default_account: Optional[str] = None
    ibkr_managed_accounts: Optional[str] = None
    
    # Logging
    log_level: str = "INFO"
    log_file: str = "/tmp/ibkr-mcp-server.log"
    
    # Reconnection
    max_reconnect_attempts: int = 5
    reconnect_delay: int = 5
    
    # Market Data
    ibkr_market_data_type: int = 3  # 1=Live, 2=Frozen, 3=Delayed, 4=Delayed Frozen
    
    # Trading Safety
    enable_live_trading: bool = False
    max_order_size: int = 1000
    require_order_confirmation: bool = True

    # === LIVE-MODE SAFETY RAILS ===
    # Active when ibkr_is_paper=False. Additive on top of the existing
    # safety knobs above. The pivot loop engine + place_order both
    # consult these in live mode and refuse / circuit-break accordingly.
    #
    # Daily realized P&L floor. When today's cumulative realized P&L
    # falls below this number (e.g. -500 = "lost $500 today"), all
    # autonomous loops auto-pause and a phone alert fires. Operator
    # must manually resume. Set generously (~2× expected daily VaR);
    # this is a stop-the-bleeding rail, not a normal-day signal.
    live_daily_loss_limit: float = -500.0

    # Override max_order_size in live mode. Default 100 vs 1000 paper
    # default -- forces conservative position sizing until operator
    # explicitly raises it after observation.
    live_max_order_size: int = 100

    # On the first connect in live mode, all existing pivot loops are
    # auto-paused (status='paused' in SQLite). Operator must manually
    # resume each one. Prevents the engine from immediately taking
    # trades on a freshly-flipped account with parameters tuned on
    # paper data. Set False to disable the auto-pause behavior.
    live_auto_pause_loops_on_connect: bool = True

    # Send an ntfy push for every order placement when in live mode
    # (in addition to the existing disconnect/reconnect alerts).
    # In paper mode this is off -- too noisy.
    live_ntfy_every_order: bool = True

    # When True, tools that cancel orders, stop strategies, or transmit live
    # orders return a "needs_confirmation" preview unless called with
    # confirm=true. Designed to prevent unintended destructive actions from
    # chat sessions (e.g., "stop my F swing" cancels protective stops without
    # asking). Off by default — chat workflows expect single-shot tool calls.
    require_confirmation_for_destructive_tools: bool = False
    
    # MCP Server
    mcp_server_name: str = "ibkr-mcp"
    mcp_server_version: str = "1.0.0"

    # Layer 5b — HTTP transport
    mcp_bind_host: str = "127.0.0.1"
    mcp_bind_port: int = 8765
    mcp_auth_token: Optional[str] = None

    # Phone alerts via ntfy.sh.
    #
    # Two events are wired today:
    #   * daemon loses its IBKR connection (from ibkr_mcp_server.client)
    #   * daemon HTTP becomes unresponsive (from scripts/ibkr-watchdog.sh)
    #
    # The watchdog reads NTFY_URL/NTFY_TOPIC straight out of the .env file —
    # keep the names in sync if you rename here.
    #
    # Topic names are PUBLIC: anyone who knows the topic can read its
    # messages. Pick something unguessable (e.g. `ibkr-<8 random hex>`).
    notify_enabled: bool = False
    ntfy_url: str = "https://ntfy.sh"
    ntfy_topic: Optional[str] = None

    # Layer 7 — in-house chat wrapper (the /chat endpoint on the HTTP
    # transport). Calls Anthropic API directly with our own system prompt
    # so the consumer-product safety overlay (which refuses to call
    # destructive trading tools) is not in the path. Off unless an API
    # key is configured.
    #
    # Cost: ~$0.01-0.02 per chat message at Sonnet pricing. Set a spend
    # cap in the Anthropic console.
    chat_enabled: bool = False
    anthropic_api_key: Optional[str] = None
    anthropic_model: str = "claude-sonnet-4-5"
    # Hard cap on the agent loop so a runaway tool-call cycle can't burn
    # tokens forever. Each iteration is one Anthropic API call.
    chat_max_iterations: int = 12
    # Where the SQLite conversation store lives. Defaults to a file
    # alongside the other daemon state files. Override only if you
    # want to put it on a different volume or share with another
    # process.
    chat_db_path: str = "/home/trader/ibkr-mcp-server/chat.db"

    # Optional short PIN for unlocking the chat UI on a new device
    # without pasting the full 64-char MCP_AUTH_TOKEN. When set, the
    # /chat page prompts for this PIN instead of the token; on success
    # the server returns the bearer token and the page saves it to
    # localStorage. Bearer token still works as a parallel auth path
    # (Claude Desktop, curl, etc. unchanged).
    #
    # Plaintext storage is fine -- the PIN's security floor is the
    # same as the bearer token, which already lives in this file.
    # Brute-force resistance comes from the rate-limit + lockout
    # logic, not from the storage format.
    chat_pin: Optional[str] = None

    # How often the background task records a portfolio equity snapshot
    # into chat.db. 0 disables snapshotting entirely. Default 1 hour --
    # gives a clean equity curve without producing thousands of rows
    # per day. The chart tool reads from these rows.
    portfolio_snapshot_interval_seconds: int = 3600
    
    @field_validator('ibkr_managed_accounts')
    @classmethod
    def parse_managed_accounts(cls, v) -> Optional[List[str]]:
        """Parse comma-separated managed accounts."""
        if v:
            return [acc.strip() for acc in v.split(',') if acc.strip()]
        return None
    
    @field_validator('log_level')
    @classmethod
    def validate_log_level(cls, v):
        """Validate log level."""
        valid_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
        if v.upper() not in valid_levels:
            raise ValueError(f'Log level must be one of: {valid_levels}')
        return v.upper()
    
    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        # The .env file is shared with the Gateway container, which needs
        # its own creds (TWS_USERID, TWS_PASSWORD, etc.) plus a handful of
        # gnzsnz-image-specific vars. Don't reject those on load.
        "extra": "ignore",
    }


# Global settings instance
settings = Settings()
