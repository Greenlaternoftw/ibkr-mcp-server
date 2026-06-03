"""Server-side chart rendering.

Tools like ``get_chart`` / ``get_swing_visualization`` return PNG bytes
that get base64-encoded into MCP ``ImageContent`` blocks. The chat UI
renders them inline; Claude itself also sees them and can reason about
what it's looking at on follow-up turns.

Design choices:

  * **Matplotlib only.** No mplfinance. OHLC candlesticks aren't hard
    to draw with bare rectangles+lines and skipping the dep means one
    less surprise at install time.
  * **Headless backend** (``Agg``) forced before any pyplot import --
    the daemon has no display server.
  * **Lazy import.** Every entry point imports matplotlib inside the
    function so the module loads cleanly (and tests run) when
    matplotlib isn't installed.
  * **Dark theme by default** to match the chat UI; light theme
    available via ``theme="light"`` for users who prefer it.
  * **Sized for phone screens.** Default 800x500px renders crisply
    on a phone Retina display without wasting bytes; user can override.

All rendering happens synchronously inside the chat-agent loop. PNG
encode is the slow step (~100-300ms for a typical chart); fast enough
that we don't bother offloading to a worker.
"""

from __future__ import annotations

import io
import logging
from typing import Any, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# --- theme palette ---------------------------------------------------------

# Two palettes, matched to the chat UI's dark/light CSS so charts feel
# continuous with the page they're rendered into.
_THEMES = {
    "dark": {
        "bg": "#0f172a",
        "panel": "#1e293b",
        "border": "#334155",
        "text": "#e2e8f0",
        "text_dim": "#94a3b8",
        "accent": "#38bdf8",
        "up": "#10b981",       # bullish candle / above-cost
        "down": "#f87171",     # bearish candle / below-cost
        "sma_short": "#fbbf24",
        "sma_long": "#a78bfa",
        "marker": "#38bdf8",
    },
    "light": {
        "bg": "#ffffff",
        "panel": "#f8fafc",
        "border": "#cbd5e1",
        "text": "#0f172a",
        "text_dim": "#475569",
        "accent": "#0284c7",
        "up": "#059669",
        "down": "#dc2626",
        "sma_short": "#d97706",
        "sma_long": "#7c3aed",
        "marker": "#0284c7",
    },
}


def _setup_mpl(theme: str = "dark"):
    """Lazy-import matplotlib + apply theme. Returns (plt, palette).

    Imports are inside the function so the module is importable without
    matplotlib (tests mock the call site instead of installing the dep).
    """
    import matplotlib
    matplotlib.use("Agg", force=True)  # no display; safe in daemons / tests
    import matplotlib.pyplot as plt

    palette = _THEMES.get(theme, _THEMES["dark"])
    # Apply consistent styling globally for this rendering call. We use
    # plt.rcParams rather than a stylesheet so the same module can render
    # both themes within one process without interference.
    plt.rcParams.update({
        "figure.facecolor": palette["bg"],
        "axes.facecolor": palette["bg"],
        "axes.edgecolor": palette["border"],
        "axes.labelcolor": palette["text_dim"],
        "axes.titlecolor": palette["text"],
        "axes.titlesize": 11,
        "axes.titleweight": "600",
        "axes.grid": True,
        "grid.color": palette["border"],
        "grid.alpha": 0.3,
        "grid.linewidth": 0.5,
        "xtick.color": palette["text_dim"],
        "ytick.color": palette["text_dim"],
        "xtick.labelsize": 8,
        "ytick.labelsize": 8,
        "font.family": "sans-serif",
        "font.sans-serif": ["SF Pro Text", "Helvetica Neue", "Arial", "DejaVu Sans"],
        "text.color": palette["text"],
    })
    return plt, palette


def _fig_to_png_bytes(fig, dpi: int = 130) -> bytes:
    """Render a matplotlib Figure to PNG bytes and close it.

    Closing is important -- matplotlib otherwise leaks the figure into
    a global registry and the daemon's memory grows linearly with chart
    requests.
    """
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    # Import here to avoid load-time dep.
    import matplotlib.pyplot as plt
    plt.close(fig)
    return buf.getvalue()


# --- OHLC candlestick chart -----------------------------------------------


def render_ohlc_chart(
    bars: Any,                                    # pandas.DataFrame
    *,
    symbol: str,
    sma_periods: Sequence[int] = (20, 50),
    overlays: Optional[Sequence[dict]] = None,    # extra horizontal lines or markers
    theme: str = "dark",
    width_px: int = 800,
    height_px: int = 500,
) -> bytes:
    """Render an OHLC candlestick chart with optional moving averages.

    ``bars`` is the DataFrame returned by ``IBKRClient.get_historical_bars``
    (columns: ``date``, ``open``, ``high``, ``low``, ``close``, ``volume``).
    The DataFrame's existing index is irrelevant -- we plot against the
    ``date`` column.

    ``overlays`` is an optional list of dicts that extend the chart with
    strategy context (cost basis, trail stop, fill markers, etc.). Each
    overlay dict has a ``type``:
      * ``{"type": "hline", "y": 13.36, "label": "cost basis", "color": "..."}``
      * ``{"type": "marker", "x": "2026-04-15", "y": 15.50, "label": "fill", "color": "..."}``

    Returns raw PNG bytes; caller base64-encodes for MCP ImageContent.
    """
    plt, palette = _setup_mpl(theme)
    overlays = overlays or []

    # Figure sized to roughly match phone-display dimensions at 130 DPI.
    fig_w = width_px / 130.0
    fig_h = height_px / 130.0
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))

    # Candlestick rendering. We draw the wick (high->low line) and body
    # (open->close rect) manually -- matplotlib doesn't ship candlesticks
    # in the core lib, and we don't want the mplfinance dep.
    opens = bars["open"].to_numpy()
    highs = bars["high"].to_numpy()
    lows = bars["low"].to_numpy()
    closes = bars["close"].to_numpy()

    # Use integer x positions so candles align on a regular grid; we'll
    # re-label the x ticks with actual dates below.
    import numpy as np
    xs = np.arange(len(bars))

    bullish = closes >= opens
    body_low = np.minimum(opens, closes)
    body_high = np.maximum(opens, closes)

    # Wicks: thin vertical lines from low to high.
    ax.vlines(xs, lows, highs,
              color=palette["text_dim"], linewidth=0.7, alpha=0.7)
    # Bodies: rectangles. Width 0.7 leaves gaps between candles.
    body_width = 0.7
    for x, lo, hi, bull in zip(xs, body_low, body_high, bullish):
        color = palette["up"] if bull else palette["down"]
        height = max(hi - lo, (closes.max() - lows.min()) * 0.0008)  # min 0.08% bar so flat candles still show
        ax.add_patch(plt.Rectangle(
            (x - body_width / 2, lo), body_width, height,
            color=color, alpha=0.85, linewidth=0,
        ))

    # Moving averages.
    for period, color_key in zip(sma_periods, ("sma_short", "sma_long")):
        if len(bars) >= period:
            sma = bars["close"].rolling(period).mean()
            ax.plot(xs, sma.to_numpy(), color=palette[color_key],
                    linewidth=1.3, alpha=0.85, label=f"SMA{period}")

    # Strategy overlays (cost basis, trail stop, fills, etc.).
    for ov in overlays:
        if ov.get("type") == "hline":
            ax.axhline(y=ov["y"], color=ov.get("color") or palette["accent"],
                       linewidth=1.2, alpha=0.7, linestyle="--",
                       label=ov.get("label"))
        elif ov.get("type") == "marker":
            # x can be a date string or an integer index; resolve to integer.
            x = ov["x"]
            if isinstance(x, str):
                # Match the date column. If not found, skip rather than crash.
                matches = bars.index[bars["date"].astype(str).str.startswith(x)]
                if len(matches) == 0:
                    continue
                x = int(matches[0])
            ax.scatter([x], [ov["y"]],
                       color=ov.get("color") or palette["marker"],
                       s=60, zorder=5, edgecolors=palette["bg"], linewidths=1.5,
                       label=ov.get("label"))

    # X-tick relabel: show ~6 actual dates spread across the range.
    n_ticks = min(6, len(bars))
    if n_ticks >= 2:
        tick_xs = np.linspace(0, len(bars) - 1, n_ticks).astype(int)
        labels = [str(bars["date"].iloc[i])[:10] for i in tick_xs]
        ax.set_xticks(tick_xs)
        ax.set_xticklabels(labels, rotation=0, ha="center")

    # Title + legend.
    last_close = float(closes[-1])
    first_close = float(closes[0])
    pct = (last_close - first_close) / first_close * 100 if first_close else 0
    title_color = palette["up"] if pct >= 0 else palette["down"]
    title = f"{symbol}  ·  ${last_close:.2f}  ·  {pct:+.1f}% over {len(bars)} bars"
    ax.set_title(title, color=title_color, loc="left", pad=8)

    if sma_periods or overlays:
        leg = ax.legend(loc="upper left", frameon=False, fontsize=8,
                        labelcolor=palette["text_dim"])
        if leg:
            for text in leg.get_texts():
                text.set_color(palette["text_dim"])

    # Tighten margins.
    ax.margins(x=0.01)
    ax.set_xlim(-0.5, len(bars) - 0.5)
    fig.tight_layout(pad=0.5)

    return _fig_to_png_bytes(fig)


# --- equity curve --------------------------------------------------------


def render_equity_curve(
    snapshots: list[dict],
    *,
    account: str,
    theme: str = "dark",
    width_px: int = 800,
    height_px: int = 400,
) -> bytes:
    """Render a portfolio-equity curve from chat.db snapshot rows.

    ``snapshots`` is a list of dicts with ``timestamp`` (ISO 8601) and
    ``net_liquidation`` (the headline number). Other columns
    (``total_cash``, ``positions_value``) are ignored here -- we keep
    this chart purposefully focused on one line so it stays legible.

    Caller is expected to pass at least 2 snapshots; with 1 the line
    will be a single dot, which is correct but not interesting.
    """
    plt, palette = _setup_mpl(theme)

    fig_w = width_px / 130.0
    fig_h = height_px / 130.0
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))

    # Parse timestamps. We accept either ISO with timezone (the
    # _utc_now_iso format) or naive datetime strings -- pandas handles
    # both. matplotlib understands datetime64 objects directly.
    import pandas as pd
    times = pd.to_datetime([s["timestamp"] for s in snapshots])
    values = [float(s["net_liquidation"]) for s in snapshots]

    first = values[0]
    last = values[-1]
    pct = (last - first) / first * 100 if first else 0
    up = last >= first
    line_color = palette["up"] if up else palette["down"]

    # Line + light fill underneath to give the chart some weight.
    ax.plot(times, values, color=line_color, linewidth=1.8, alpha=0.95)
    ax.fill_between(times, values, min(values), color=line_color, alpha=0.12)

    # Title shows total change and current value.
    title = (
        f"{account}  ·  ${last:,.0f}  ·  {pct:+.2f}% "
        f"({len(snapshots)} snapshots over "
        f"{(times[-1] - times[0]).total_seconds() / 86400:.1f} days)"
    )
    ax.set_title(title, color=line_color, loc="left", pad=8)

    # Y axis formatted as currency.
    from matplotlib.ticker import FuncFormatter
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v:,.0f}"))

    # X axis: short date labels, auto-spaced.
    fig.autofmt_xdate(rotation=0, ha="center")

    ax.margins(x=0.01)
    fig.tight_layout(pad=0.5)
    return _fig_to_png_bytes(fig)
