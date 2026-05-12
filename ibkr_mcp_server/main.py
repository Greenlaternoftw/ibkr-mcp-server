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
@click.option('--log-level', default=settings.log_level, help='Logging level')
@click.option('--log-file', default=settings.log_file, help='Log file path')
def cli(test: bool, log_level: str, log_file: str):
    """IBKR MCP Server - Interactive Brokers integration for Claude."""
    setup_logging(log_level, log_file, mcp_mode=not test)
    
    if test:
        # Run connection test
        success = asyncio.run(test_connection())
        sys.exit(0 if success else 1)
    else:
        # Run the server
        asyncio.run(run_server())


async def main():
    """Main entry point when called as module."""
    setup_logging(settings.log_level, settings.log_file, mcp_mode=True)
    await run_server()


if __name__ == "__main__":
    cli()
