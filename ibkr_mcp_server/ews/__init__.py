"""Portfolio Early Warning System (EWS).

Adapts the EWS integration brief to the daemon's architecture:

  - brief assumes a React site with browser-only push + client-side
    Claude calls; we run the scan loop server-side in the daemon, call
    Anthropic through the existing server-side client, and push via ntfy
    (true OS push even when the browser is closed).

Modules:
  - signals.py   -- the six signal fetchers (EDGAR free, Unusual Whales
                    optional, news via the existing yfinance lexicon)
  - analyzer.py  -- Claude analysis -> structured recommendation
  - monitor.py   -- the periodic scan loop + ntfy dispatch
  - ics.py       -- calendar-reminder (.ics) generation
  - persistence.py -- the SQLite alert feed + scan audit

Everything degrades gracefully: no UW key -> free signals only; no
Anthropic key -> EWS disabled; a failing source -> empty signal, never
a crash in the scan loop.
"""

SEVERITY_ORDER = {"CRITICAL": 3, "HIGH": 2, "MEDIUM": 1, "INFO": 0}


def severity_at_least(sev: str, floor: str) -> bool:
    """True if `sev` is at or above the `floor` severity."""
    return SEVERITY_ORDER.get((sev or "").upper(), -1) >= SEVERITY_ORDER.get(
        (floor or "INFO").upper(), 0
    )
