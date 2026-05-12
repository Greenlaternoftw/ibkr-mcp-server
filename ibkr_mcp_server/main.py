"""Main entry point for IBKR MCP Server."""

import asyncio
import logging
import signal
import sys
from typing import Optional

import click
from mcp.server.stdio import stdio_server
from rich.console import Console
from rich.logging import RichHandler

from .client import ibkr_client
from .config import settings
from .tools import TOOLS, server


console = Console()


class GracefulKiller:
    """Handle shutdown signals gracefully."""
    
    def __init__(self):
        self.kill_now = False
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
    
    def _handle_signal(self, signum, frame):
        # Only log to stderr when running as MCP server
        logger = logging.getLogger(__name__)
        logger.info(f"Received signal {signum}, shutting down gracefully...")
        self.kill_now = True


def setup_logging(level: str = "INFO", log_file: Optional[str] = None, mcp_mode: bool = False):
    """Setup logging configuration."""
    handlers = []
    
    # Always add file handler if specified
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(
            logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        )
        handlers.append(file_handler)
    
    # Only add console handler if NOT in MCP mode
    if not mcp_mode:
        handlers.append(RichHandler(console=console, show_time=True, show_path=False))
    else:
        # In MCP mode, log to stderr instead of stdout
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(
            logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        )
        handlers.append(stderr_handler)
    
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(message)s",
        datefmt="[%X]",
        handlers=handlers,
        force=True  # Override any existing configuration
    )
    
    # Reduce noise from ib_async
    logging.getLogger('ib_async').setLevel(logging.WARNING)


async def test_connection():
    """Test IBKR connection and basic functionality."""
    console.print("[bold blue]🧪 Testing IBKR MCP Server...[/bold blue]")
    
    try:
        # Test connection
        console.print("📡 Testing IBKR connection...")
        await ibkr_client.connect()
        console.print("✅ Connection successful!")
        
        # Test basic functionality
        console.print("🔍 Testing basic functionality...")
        accounts_info = await ibkr_client.get_accounts()
        available = accounts_info.get("available_accounts") or []
        console.print(f"📊 Found {len(available)} accounts: {available}")

        # Tool registry sanity check
        console.print("🛠️ Testing MCP tools...")
        console.print(f"⚙️ Loaded {len(TOOLS)} tools: {[t.name for t in TOOLS]}")
        
        console.print("[bold green]✅ All tests passed![/bold green]")
        return True
        
    except Exception as e:
        console.print(f"[bold red]❌ Test failed: {e}[/bold red]")
        return False
    finally:
        await ibkr_client.disconnect()


async def _patient_initial_connect(timeout_seconds: int = 240) -> None:
    """Try to connect to IBKR with extended patience.

    Daemon startup commonly races Gateway's own login sequence (especially in
    Docker compose, where both containers start at the same time and Gateway
    takes 30-60 seconds to authenticate before its API port responds). The
    `@retry_on_failure(3)` on `connect` only gives ~7 seconds total, which
    isn't enough. Here we retry with a longer ceiling so the daemon waits
    patiently for Gateway to come up rather than crash-looping.
    """
    import time
    logger = logging.getLogger(__name__)
    deadline = time.time() + timeout_seconds
    attempt = 0
    while True:
        attempt += 1
        try:
            await ibkr_client.connect()
            logger.info(f"connected to IBKR Gateway (attempt {attempt})")
            return
        except Exception as e:
            remaining = int(deadline - time.time())
            if remaining <= 0:
                logger.error(f"giving up on initial connect after {timeout_seconds}s")
                raise
            logger.warning(
                f"initial connect attempt {attempt} failed ({e!s}); "
                f"retrying in 10s (~{remaining}s remaining)"
            )
            await asyncio.sleep(10)


async def run_daemon_http():
    """Layer 5b — daemon + HTTP MCP transport.

    Same lifecycle as `run_daemon`, but also starts a Starlette HTTP server
    on `MCP_BIND_HOST:MCP_BIND_PORT` so remote MCP clients can call tools.
    The HTTP server runs the protocol over streamable HTTP at `/mcp` with
    `/healthz` for unauthenticated monitoring probes.
    """
    from .http_server import run_http_server

    logger = logging.getLogger(__name__)
    logger.info("=== IBKR MCP daemon (HTTP transport) starting ===")

    try:
        await _patient_initial_connect()

        reconciled = await ibkr_client.reconcile_on_startup()
        logger.info(f"startup reconciliation: {reconciled}")

        resumed = await ibkr_client.resume_strategies_from_state()
        logger.info(
            f"resumed strategies: reversal={resumed['reversal']} swing={resumed['swing']}"
        )

        await run_http_server(
            host=settings.mcp_bind_host,
            port=settings.mcp_bind_port,
            auth_token=settings.mcp_auth_token,
        )

    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("daemon shutdown requested")
    except Exception as e:
        logger.exception(f"daemon error: {e}")
        raise
    finally:
        try:
            await ibkr_client.disconnect()
        except Exception:
            pass
        logger.info("=== IBKR MCP daemon (HTTP transport) stopped ===")


async def run_daemon():
    """Layer 5a — run as a persistent daemon.

    Connects to IBKR, restores any active strategies from state files, subscribes
    to fill events (already wired up in `IBKRClient.connect`), and keeps the
    process alive indefinitely. Designed to run under systemd or Docker.

    No stdio MCP server is started in this mode — daemon-only. Use the regular
    `--test` path or future `--transport http` (Layer 5b) for control.
    """
    logger = logging.getLogger(__name__)
    logger.info("=== IBKR MCP daemon starting ===")

    try:
        await _patient_initial_connect()

        reconciled = await ibkr_client.reconcile_on_startup()
        logger.info(f"startup reconciliation: {reconciled}")

        resumed = await ibkr_client.resume_strategies_from_state()
        logger.info(
            f"resumed strategies: reversal={resumed['reversal']} swing={resumed['swing']}"
        )

        # Stay alive. Periodically probe the connection so we notice silent
        # disconnects that the disconnect handler missed.
        while True:
            await asyncio.sleep(300)  # 5-minute heartbeat
            if not ibkr_client.is_connected():
                logger.warning("connection check failed, attempting reconnect")
                try:
                    await ibkr_client.connect()
                    await ibkr_client.reconcile_on_startup()
                except Exception as e:
                    logger.error(f"reconnect attempt failed: {e}")
            else:
                logger.debug("heartbeat: connection ok")

    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("daemon shutdown requested")
    except Exception as e:
        logger.exception(f"daemon error: {e}")
        raise
    finally:
        try:
            await ibkr_client.disconnect()
        except Exception:
            pass
        logger.info("=== IBKR MCP daemon stopped ===")


async def run_server():
    """Run the MCP server with connection management."""
    logger = logging.getLogger(__name__)
    
    # Note: No console.print() calls here as they interfere with MCP protocol
    logger.info("Starting IBKR MCP Server...")
    
    try:
        # Start MCP server immediately - connection will be established on demand
        logger.info("Starting MCP server...")
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options()
            )
            
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    except Exception as e:
        logger.error(f"Server error: {e}")
        raise
    finally:
        try:
            await ibkr_client.disconnect()
        except:
            pass
        logger.info("Server shutdown complete")


@click.command()
@click.option('--test', is_flag=True, help='Test connection and exit')
@click.option('--daemon', is_flag=True, help='Run as a persistent always-on daemon (Layer 5a)')
@click.option('--transport', type=click.Choice(['stdio', 'http']), default='stdio',
              help='MCP transport: stdio (default) or http (Layer 5b)')
@click.option('--log-level', default=settings.log_level, help='Logging level')
@click.option('--log-file', default=settings.log_file, help='Log file path')
def cli(test: bool, daemon: bool, transport: str, log_level: str, log_file: str):
    """IBKR MCP Server - Interactive Brokers integration for Claude."""
    # MCP stdio mode requires logging to stderr so stdout stays JSON-RPC clean.
    # Other modes (test, daemon, http transport) can use plain logging.
    mcp_stdio_mode = (transport == 'stdio' and not daemon and not test)
    setup_logging(log_level, log_file, mcp_mode=mcp_stdio_mode)

    if test:
        success = asyncio.run(test_connection())
        sys.exit(0 if success else 1)
    elif daemon and transport == 'http':
        try:
            asyncio.run(run_daemon_http())
        except KeyboardInterrupt:
            sys.exit(0)
    elif daemon:
        try:
            asyncio.run(run_daemon())
        except KeyboardInterrupt:
            sys.exit(0)
    elif transport == 'http':
        # HTTP transport without --daemon: still need to connect, but skip
        # state recovery (operator probably testing the transport itself).
        try:
            asyncio.run(_run_http_only())
        except KeyboardInterrupt:
            sys.exit(0)
    else:
        asyncio.run(run_server())


async def _run_http_only() -> None:
    """HTTP transport without the daemon lifecycle. Mainly for testing the
    transport in isolation; production uses `--daemon --transport http`."""
    from .http_server import run_http_server
    logger = logging.getLogger(__name__)
    logger.info("=== IBKR MCP HTTP transport (no daemon) starting ===")
    try:
        await ibkr_client.connect()
        await run_http_server(
            host=settings.mcp_bind_host,
            port=settings.mcp_bind_port,
            auth_token=settings.mcp_auth_token,
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        try:
            await ibkr_client.disconnect()
        except Exception:
            pass


async def main():
    """Main entry point when called as module."""
    setup_logging(settings.log_level, settings.log_file, mcp_mode=True)
    await run_server()


if __name__ == "__main__":
    cli()
