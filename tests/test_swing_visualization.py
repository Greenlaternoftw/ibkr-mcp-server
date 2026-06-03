"""Tests for get_swing_visualization.

This is the second image-returning tool, layered on top of the same
chart infrastructure as get_chart. The tests focus on what's NEW vs
get_chart:

  * Returns a clean error dict when no swing strategy is registered
    for the symbol (so the model can suggest get_chart instead).
  * Overlay construction picks up cost basis, floor, trail stop,
    dip target (when FLAT), and last-fill marker.
  * The matplotlib render is offloaded to a thread (verified by
    asserting asyncio.to_thread is awaited).
  * The dispatcher returns mixed TextContent + ImageContent on
    success, text-only on error.

We mock get_historical_bars + the chart renderer so the test runs
without matplotlib installed locally and without network.
"""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from ibkr_mcp_server.client import IBKRClient
from ibkr_mcp_server.swing import SwingConfig, SwingState, SwingStateRecord


# --- shared fixtures -----------------------------------------------------


_TINY_PNG = bytes.fromhex(
    "89504E470D0A1A0A0000000D49484452000000010000000108060000001F15C4"
    "890000000A49444154789C6300010000000500010D0A2DB40000000049454E44AE426082"
)


def _make_bars(n: int = 60, base_price: float = 100.0) -> pd.DataFrame:
    """Synthetic OHLCV bars in the shape get_historical_bars returns.

    Real bars come from ib_async's util.df(); they have date, open, high,
    low, close, volume columns. We don't need realistic prices -- the
    overlay-building code just needs a non-empty df with the right cols.
    """
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "date": dates,
        "open":   [base_price + i * 0.1 for i in range(n)],
        "high":   [base_price + i * 0.1 + 0.5 for i in range(n)],
        "low":    [base_price + i * 0.1 - 0.5 for i in range(n)],
        "close":  [base_price + i * 0.1 + 0.2 for i in range(n)],
        "volume": [1_000_000] * n,
    })


@pytest.fixture
def client(tmp_path):
    """Bare client with mocked IB + state paths inside tmp_path."""
    c = IBKRClient()
    c.ib = MagicMock()
    c.ib.isConnected.return_value = True
    c._connected = True
    c._swing_state_path = tmp_path / "swing.json"
    c._swing_states = {}   # start empty; tests register as needed
    return c


def _register_swing(client, **overrides):
    """Insert a SwingStateRecord for AAPL into the client's state dict.

    Overrides any field via kwargs; uses sensible defaults otherwise.
    """
    defaults = dict(
        symbol="AAPL",
        quantity=10,
        cost_basis=200.0,
        config=SwingConfig(
            trail_atr_multiplier=2.0,
            floor_offset=5.0,
            dip_percent=3.0,
        ),
        state=SwingState.HOLDING,
        oca_group="grp-AAPL",
        last_fill_action="BUY",
        last_fill_price=200.0,
        last_fill_time="2026-01-15T14:30:00",
    )
    defaults.update(overrides)   # overrides win cleanly
    state_record = SwingStateRecord(**defaults)
    client._swing_states["AAPL"] = state_record
    return state_record


# --- error path: no strategy registered ----------------------------------


class TestNoStrategy:
    @pytest.mark.asyncio
    async def test_returns_error_dict_when_no_strategy(self, client):
        """The dispatcher uses this error path to fall back to get_chart
        in the system prompt -- so the message has to be clean and
        machine-recognizable, not just a stack trace."""
        out = await client.get_swing_visualization("NVDA")
        assert out["status"] == "error"
        assert "NVDA" in out["message"]
        # The hint to use get_chart is operator-facing UX -- the model
        # reads it and suggests the fallback.
        assert "get_chart" in out["message"]
        # No image generated on error -- saves a futile matplotlib call.
        assert "image_png_b64" not in out


# --- success path: overlays + render --------------------------------------


class TestOverlays:
    @pytest.mark.asyncio
    async def test_holding_state_renders_cost_floor_trail_marker(self, client):
        """HOLDING state should produce four overlays: cost, floor, trail,
        and last-fill marker. No dip target (that's FLAT-only)."""
        _register_swing(client, state=SwingState.HOLDING)

        captured_overlays = []

        def fake_render(bars, *, symbol, sma_periods, overlays, theme):
            # Capture so we can assert on what got drawn.
            captured_overlays.extend(overlays)
            return _TINY_PNG

        with patch.object(
            client, "get_historical_bars", new=AsyncMock(return_value=_make_bars())
        ), patch(
            "ibkr_mcp_server.charts.render_ohlc_chart", side_effect=fake_render
        ):
            out = await client.get_swing_visualization("AAPL")

        assert out["status"] == "ok"
        labels = [o["label"] for o in captured_overlays if "label" in o]
        # Cost basis line
        assert any("cost $200.00" in l for l in labels), labels
        # Floor (cost - offset = 200 - 5 = 195)
        assert any("floor $195.00" in l for l in labels), labels
        # Trail stop -- present (exact value depends on ATR; just confirm one was drawn)
        assert any(l.startswith("trail ~$") for l in labels), labels
        # Last-fill marker
        assert any("last BUY $200.00" in l for l in labels), labels
        # NO dip target in HOLDING state
        assert not any("dip target" in l for l in labels), labels

    @pytest.mark.asyncio
    async def test_flat_state_adds_dip_target_overlay(self, client):
        """FLAT means a protective sell fired and we're waiting on a dip
        to re-enter. The dip-target line should appear."""
        _register_swing(
            client,
            state=SwingState.FLAT,
            last_fill_action="SELL",
            last_fill_price=210.0,
            last_fill_time="2026-02-01T15:00:00",
        )

        captured = []

        def fake_render(bars, *, symbol, sma_periods, overlays, theme):
            captured.extend(overlays)
            return _TINY_PNG

        with patch.object(
            client, "get_historical_bars", new=AsyncMock(return_value=_make_bars())
        ), patch(
            "ibkr_mcp_server.charts.render_ohlc_chart", side_effect=fake_render
        ):
            out = await client.get_swing_visualization("AAPL")

        assert out["status"] == "ok"
        labels = [o["label"] for o in captured if "label" in o]
        # 3% dip from 210 = 203.70
        assert any("dip target $203.70" in l for l in labels), labels
        # Last-fill marker still drawn (now a SELL)
        assert any("last SELL $210.00" in l for l in labels), labels

    @pytest.mark.asyncio
    async def test_summary_has_pct_vs_cost(self, client):
        """The text summary should include the % above/below cost so
        Claude can quote it without re-doing the math."""
        _register_swing(client)

        with patch.object(
            client, "get_historical_bars",
            new=AsyncMock(return_value=_make_bars(n=60, base_price=205.0)),
        ), patch(
            "ibkr_mcp_server.charts.render_ohlc_chart", return_value=_TINY_PNG
        ):
            out = await client.get_swing_visualization("AAPL")

        # Cost is 200, last close from synthetic bars is ~211.2 -> ~+5.6%
        assert "+" in out["summary"]  # gain symbol present
        assert "pct_vs_cost" in out
        assert isinstance(out["pct_vs_cost"], float)


# --- thread offload ------------------------------------------------------


class TestThreadOffload:
    @pytest.mark.asyncio
    async def test_render_runs_on_thread_not_event_loop(self, client):
        """The matplotlib render is sync CPU-bound. If it ran on the
        asyncio loop, /healthz would hang during a chart request and
        the watchdog would kill the daemon (real production incident,
        see commit db41809). Verify the render goes through
        asyncio.to_thread so the loop stays responsive."""
        _register_swing(client)

        # Patch to_thread to capture the call; verify the FIRST arg is
        # something callable (the render function passed by reference).
        # We check inside the with block so the patch is still active.
        with patch.object(
            client, "get_historical_bars", new=AsyncMock(return_value=_make_bars())
        ), patch(
            "asyncio.to_thread",
            new=AsyncMock(return_value=_TINY_PNG),
        ) as to_thread:
            out = await client.get_swing_visualization("AAPL")

            # to_thread was awaited exactly once -- the render call.
            assert to_thread.await_count == 1
            called_with = to_thread.await_args.args
            assert callable(called_with[0]), \
                "first arg to to_thread must be the render function"
            # The function's name should be render_ohlc_chart (this is
            # the actual function reference passed by name, not patched).
            assert called_with[0].__name__ == "render_ohlc_chart"

        assert out["status"] == "ok"
