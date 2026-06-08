"""EWS calendar reminders (.ics) — brief Section 7.

Pure-Python VCALENDAR generation, no external library. Each EWS alert
can produce review-date reminders (30d/60d/90d). The frontend offers
"download .ics" per alert and "download all". This module builds the
text; the route serves it as text/calendar.

We deliberately keep it RFC-5545-minimal but cross-client safe (Apple
Calendar, Google Calendar, Outlook all parse this shape), including a
VALARM 24h before so the reminder actually fires.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional


def _fold(line: str) -> str:
    """RFC-5545 line folding at 75 octets (best-effort, ASCII)."""
    if len(line) <= 75:
        return line
    out = [line[:75]]
    rest = line[75:]
    while rest:
        out.append(" " + rest[:74])
        rest = rest[74:]
    return "\r\n".join(out)


def _esc(text: str) -> str:
    """Escape per RFC-5545 (commas, semicolons, backslashes, newlines)."""
    return (str(text or "")
            .replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace("\n", "\\n"))


def _vevent(*, uid: str, summary: str, description: str,
            date: dt.date, stamp: str) -> List[str]:
    ymd = date.strftime("%Y%m%d")
    nxt = (date + dt.timedelta(days=1)).strftime("%Y%m%d")
    return [
        "BEGIN:VEVENT",
        _fold(f"UID:{uid}"),
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{ymd}",
        f"DTEND;VALUE=DATE:{nxt}",
        _fold(f"SUMMARY:{_esc(summary)}"),
        _fold(f"DESCRIPTION:{_esc(description)}"),
        "BEGIN:VALARM",
        "TRIGGER:-PT24H",
        "ACTION:DISPLAY",
        _fold(f"DESCRIPTION:{_esc(summary)}"),
        "END:VALARM",
        "END:VEVENT",
    ]


# Review-date offsets keyed off the alert's creation date.
_REVIEW_OFFSETS = [("30d", 30), ("60d", 60), ("90d", 90)]


def _alert_reviews(alert: Dict[str, Any], now: dt.datetime) -> List[Dict[str, Any]]:
    """Turn one alert into its review-date reminder rows."""
    sym = alert.get("symbol", "?")
    action = alert.get("action", "WATCH")
    targets = alert.get("price_targets", {}) or {}
    base = now.date()
    rows = []
    for label, days in _REVIEW_OFFSETS:
        tgt = targets.get(label) or ""
        desc = (f"{action} review for {sym}. "
                f"{alert.get('summary', '')} "
                f"{label} target: {tgt}. "
                "Informational only — not investment advice.")
        rows.append({
            "uid": f"ews-{sym}-{alert.get('id', 'x')}-{label}@ibkr-mcp",
            "summary": f"{sym} {action} — {label} Review",
            "description": desc,
            "date": base + dt.timedelta(days=days),
        })
    return rows


def build_ics(alerts: List[Dict[str, Any]], *, now: Optional[dt.datetime] = None) -> str:
    """Build a VCALENDAR string covering all review dates for `alerts`.

    `now` is injectable for deterministic tests (the daemon forbids
    argless datetime.now in some contexts; callers pass it explicitly).
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//ibkr-mcp//EWS//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    for alert in alerts:
        for rv in _alert_reviews(alert, now):
            lines += _vevent(uid=rv["uid"], summary=rv["summary"],
                             description=rv["description"], date=rv["date"],
                             stamp=stamp)
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
