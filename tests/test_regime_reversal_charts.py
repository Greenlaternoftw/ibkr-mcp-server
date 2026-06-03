"""Tests for get_regime_chart and get_reversal_visualization.

Phase 3.3 tools layered on the same chart infra as get_chart /
get_swing_visualization. Tests focus on the new behaviors:

  * get_regime_chart computes the three gates against fetched bars
    and bakes the verdict into the title; insufficient-bar conditions
    are handled gracefully instead of crashing.
  * get_reversal_visualization builds tranche markers from the
    ReversalState's filled_tranches list and computes the
    share-weighted average fill price + unrealized P&L.
  * Both tools error cleanly when their preconditions aren't met
    (no reversal state / not enough bars for regime).
  * Both go through asyncio.to_thread for the matplotlib render so
    the event loop stays responsive.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from ibkr_mcp_server.client import IBKRClient
from ibkr_mcp_server.reversal import (
    FilledTranche,
    ReversalConfig,
    ReversalState,
    ReversalStatus,
)


_TINY_PNG = bytes.fromhex(
    "89504E470D0A1A0A0000000D49484452000000010000000108060000001F15C4"
    "890000000A49444154789C6300010000000500010D0A2DB40000000049454E44AE426082"
)


def _make_bars(n: int, base: float = 100.0) -> pd.DataFrame:
    """Synthetic OHLCV in the get_historical_bars shape."""
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "date": dates,
        "open":   [base + i * 0.1 for i in range(n)],
        "high":   [base + i * 0.1 + 0.5 for i in range(n)],
        "low":    [base + i * 0.1 - 0.5 for i in range(n)],
        "close":  [base + i * 0.1 + 0.2 for i in range(n)],
        "volume": [1_000_000] * n,
    })


@pytest.fixture
def client(tmp_path):
    c = IBKRClient()
    c.ib = MagicMock()
    c.ib.isConnected.return_value = True
    c._connected = True
    c._reversal_state_path = tmp_path / "reversal.json"
    c._swing_state_path = tmp_path / "swing.json"
    c._reversal_states = {}
    c._swing_states = {}
    return c


# --- get_regime_chart ---------------------------------------------------


class TestRegimeChart:
    @pytest.mark.asyncio
    async def test_renders_with_enabled_verdict_in_title(self, client):
        """Mock gate evaluation to return all-passes, verify the chart
        title carries ENABLED and the result reports regime_enabled=True."""
        captured = {}

        def fake_render(bars, *, symbol, sma_periods, theme):
            captured["symbol_arg"] = symbol
            return _TINY_PNG

        # Mock evaluate_gates to all-pass (regime ENABLED).
        all_pass_gates = {
            "trend_rising": {"pass": True, "sma50_today": 105.0, "sma50_5_ago": 103.0},
            "trend_strength_ok": {"pass": True, "adx": 18.5, "threshold": 25.0},
            "volatility_calm": {"pass": True, "atr_pct": 0.012, "atr_pct_avg": 0.018},
        }

        with patch.object(
            client, "get_historical_bars",
            new=AsyncMock(return_value=_make_bars(250)),
        ), patch(
            "ibkr_mcp_server.regime.evaluate_gates", return_value=all_pass_gates,
        ), patch(
            "ibkr_mcp_server.charts.render_ohlc_chart", side_effect=fake_render,
        ):
            out = await client.get_regime_chart("AAPL")

        assert out["status"] == "ok"
        assert out["regime_enabled"] is True
        assert out["verdict"] == "ENABLED"
        # Title passed to renderer should include ENABLED so the operator
        # can read the verdict from the image alone.
        assert "ENABLED" in captured["symbol_arg"]

    @pytest.mark.asyncio
    async def test_disabled_verdict_names_failing_gates(self, client):
        """When some gates fail, the verdict text should name which ones,
        so Claude can quote the specific reason in chat."""
        gates_with_failures = {
            "trend_rising": {"pass": True, "sma50_today": 105.0, "sma50_5_ago": 103.0},
            "trend_strength_ok": {"pass": False, "adx": 32.0, "threshold": 25.0},
            "volatility_calm": {"pass": False, "atr_pct": 0.025, "atr_pct_avg": 0.018},
        }

        with patch.object(
            client, "get_historical_bars",
            new=AsyncMock(return_value=_make_bars(250)),
        ), patch(
            "ibkr_mcp_server.regime.evaluate_gates", return_value=gates_with_failures,
        ), patch(
            "ibkr_mcp_server.charts.render_ohlc_chart", return_value=_TINY_PNG,
        ):
            out = await client.get_regime_chart("AAPL")

        assert out["regime_enabled"] is False
        assert out["verdict"].startswith("DISABLED")
        assert "trend_strength_ok" in out["verdict"]
        assert "volatility_calm" in out["verdict"]

    @pytest.mark.asyncio
    async def test_insufficient_bars_does_not_crash(self, client):
        """If the bars are too short for the gate computation
        (raises ValueError), the chart still renders with a
        'INSUFFICIENT_DATA' verdict instead of erroring out."""
        with patch.object(
            client, "get_historical_bars",
            new=AsyncMock(return_value=_make_bars(30)),  # too few
        ), patch(
            "ibkr_mcp_server.regime.evaluate_gates",
            side_effect=ValueError("need 115 bars; got 30"),
        ), patch(
            "ibkr_mcp_server.charts.render_ohlc_chart", return_value=_TINY_PNG,
        ):
            out = await client.get_regime_chart("AAPL")

        assert out["status"] == "ok"   # still rendered
        assert out["verdict"] == "INSUFFICIENT_DATA"
        assert out["regime_enabled"] is None

    @pytest.mark.asyncio
    async def test_render_runs_on_thread(self, client):
        with patch.object(
            client, "get_historical_bars",
            new=AsyncMock(return_value=_make_bars(250)),
        ), patch(
            "asyncio.to_thread",
            new=AsyncMock(return_value=_TINY_PNG),
        ) as to_thread:
            out = await client.get_regime_chart("AAPL")
            assert to_thread.await_count == 1
            called = to_thread.await_args.args
            assert callable(called[0])
            assert called[0].__name__ == "render_ohlc_chart"
        assert out["status"] == "ok"


# --- get_reversal_visualization -----------------------------------------


def _register_reversal(client, filled=None, status=ReversalStatus.WATCHING,
                      total_dollars=30000):
    """Insert a ReversalState into the client's state dict."""
    state = ReversalState(
        symbol="TSLA",
        total_dollars=total_dollars,
        config=ReversalConfig(tranche_count=3),
        status=status,
        filled_tranches=filled or [],
    )
    client._reversal_states["TSLA"] = state
    return state


class TestReversalChart:
    @pytest.mark.asyncio
    async def test_no_reversal_returns_error(self, client):
        """Cleanly tells the user there's no active reversal so the
        agent can suggest get_chart instead."""
        out = await client.get_reversal_visualization("NVDA")
        assert out["status"] == "error"
        assert "NVDA" in out["message"]
        assert "get_chart" in out["message"]   # suggests fallback
        assert "image_png_b64" not in out

    @pytest.mark.asyncio
    async def test_filled_tranches_produce_markers_and_avg_line(self, client):
        """Each filled tranche should become a marker overlay; an
        average-fill horizontal line gets added on top."""
        filled = [
            FilledTranche(index=1, target_dollars=10000, shares=20,
                         fill_price=500.0, filled_at="2025-04-01T15:30:00"),
            FilledTranche(index=2, target_dollars=10000, shares=20,
                         fill_price=480.0, filled_at="2025-04-08T15:45:00"),
        ]
        _register_reversal(client, filled=filled,
                          status=ReversalStatus.PARTIALLY_FILLED)

        captured_overlays = []

        def fake_render(bars, *, symbol, sma_periods, overlays, theme):
            captured_overlays.extend(overlays)
            return _TINY_PNG

        with patch.object(
            client, "get_historical_bars",
            new=AsyncMock(return_value=_make_bars(180, base=470.0)),
        ), patch(
            "ibkr_mcp_server.charts.render_ohlc_chart", side_effect=fake_render,
        ):
            out = await client.get_reversal_visualization("TSLA")

        assert out["status"] == "ok"
        assert out["tranches_filled"] == 2
        assert out["total_shares"] == 40
        # Share-weighted average of (500, 20) and (480, 20) = (20*500 + 20*480)/40 = 490
        assert out["average_fill_price"] == 490.0

        labels = [o.get("label") for o in captured_overlays]
        # Both tranche markers
        assert any("T1" in (l or "") for l in labels), labels
        assert any("T2" in (l or "") for l in labels), labels
        # Average-fill line
        assert any("avg fill $490.00" in (l or "") for l in labels), labels

    @pytest.mark.asyncio
    async def test_summary_includes_unrealized_pnl(self, client):
        """Result should report unrealized_pnl_pct so the model can
        relate price to entry without doing the math."""
        filled = [
            FilledTranche(index=1, target_dollars=10000, shares=20,
                         fill_price=500.0, filled_at="2025-04-01T15:30:00"),
        ]
        _register_reversal(client, filled=filled,
                          status=ReversalStatus.PARTIALLY_FILLED)

        with patch.object(
            client, "get_historical_bars",
            # Bars end around 100 + 179*0.1 + 0.2 = ~118; with base=525,
            # ends around 543.
            new=AsyncMock(return_value=_make_bars(180, base=525.0)),
        ), patch(
            "ibkr_mcp_server.charts.render_ohlc_chart", return_value=_TINY_PNG,
        ):
            out = await client.get_reversal_visualization("TSLA")

        assert out["unrealized_pnl_pct"] is not None
        # last_close should be > 500 -> positive pnl_pct
        assert out["unrealized_pnl_pct"] > 0
        assert "+" in out["summary"]

    @pytest.mark.asyncio
    async def test_no_fills_yet_handles_gracefully(self, client):
        """A WATCHING reversal with no fills should still render the
        chart, just with no markers and no avg-fill line."""
        _register_reversal(client, filled=[], status=ReversalStatus.WATCHING)

        with patch.object(
            client, "get_historical_bars",
            new=AsyncMock(return_value=_make_bars(180)),
        ), patch(
            "ibkr_mcp_server.charts.render_ohlc_chart", return_value=_TINY_PNG,
        ):
            out = await client.get_reversal_visualization("TSLA")

        assert out["status"] == "ok"
        assert out["tranches_filled"] == 0
        assert out["total_shares"] == 0
        assert out["average_fill_price"] is None
        assert out["unrealized_pnl_pct"] is None
        assert "No fills yet" in out["summary"]

    @pytest.mark.asyncio
    async def test_render_runs_on_thread(self, client):
        _register_reversal(client)
        with patch.object(
            client, "get_historical_bars",
            new=AsyncMock(return_value=_make_bars(180)),
        ), patch(
            "asyncio.to_thread",
            new=AsyncMock(return_value=_TINY_PNG),
        ) as to_thread:
            out = await client.get_reversal_visualization("TSLA")
            assert to_thread.await_count == 1
            assert to_thread.await_args.args[0].__name__ == "render_ohlc_chart"
        assert out["status"] == "ok"
