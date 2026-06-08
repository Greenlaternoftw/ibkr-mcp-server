"""EWS unit tests — deterministic logic only (no network / no IBKR).

Covers: severity ordering, ICS generation, analyzer response coercion +
JSON stripping + HOLD fallback. The signal fetchers and the scan loop
are integration-tested separately (they need network / a live client).
"""

import datetime as dt

from ibkr_mcp_server.ews import severity_at_least
from ibkr_mcp_server.ews import ics
from ibkr_mcp_server.ews import analyzer


class TestSeverityOrdering:
    def test_critical_beats_high(self):
        assert severity_at_least("CRITICAL", "HIGH")

    def test_high_meets_high_floor(self):
        assert severity_at_least("HIGH", "HIGH")

    def test_medium_below_high(self):
        assert not severity_at_least("MEDIUM", "HIGH")

    def test_info_below_high(self):
        assert not severity_at_least("INFO", "HIGH")

    def test_case_insensitive(self):
        assert severity_at_least("critical", "high")

    def test_unknown_severity_never_pushes(self):
        assert not severity_at_least("BOGUS", "HIGH")


class TestICS:
    def _alert(self, **kw):
        base = {
            "id": 1, "symbol": "AVEX", "action": "SELL",
            "summary": "S-1 filed; PE backer selling.",
            "price_targets": {"30d": "$35", "60d": "$30", "90d": "$28"},
        }
        base.update(kw)
        return base

    def test_build_ics_has_calendar_envelope(self):
        out = ics.build_ics([self._alert()],
                            now=dt.datetime(2026, 6, 8, tzinfo=dt.timezone.utc))
        assert out.startswith("BEGIN:VCALENDAR")
        assert out.rstrip().endswith("END:VCALENDAR")
        assert "VERSION:2.0" in out

    def test_three_review_events_per_alert(self):
        out = ics.build_ics([self._alert()],
                            now=dt.datetime(2026, 6, 8, tzinfo=dt.timezone.utc))
        assert out.count("BEGIN:VEVENT") == 3  # 30d / 60d / 90d
        assert out.count("BEGIN:VALARM") == 3  # one alarm each

    def test_review_dates_are_offset_from_now(self):
        now = dt.datetime(2026, 6, 8, tzinfo=dt.timezone.utc)
        out = ics.build_ics([self._alert()], now=now)
        # 30 days after 2026-06-08 = 2026-07-08
        assert "DTSTART;VALUE=DATE:20260708" in out
        # 90 days = 2026-09-06
        assert "DTSTART;VALUE=DATE:20260906" in out

    def test_special_chars_escaped(self):
        out = ics.build_ics(
            [self._alert(summary="Risk; sell, hedge")],
            now=dt.datetime(2026, 6, 8, tzinfo=dt.timezone.utc))
        # semicolons and commas in text must be backslash-escaped
        assert "Risk\\; sell\\, hedge" in out

    def test_empty_alerts_still_valid_calendar(self):
        out = ics.build_ics([], now=dt.datetime(2026, 6, 8, tzinfo=dt.timezone.utc))
        assert "BEGIN:VCALENDAR" in out and "END:VCALENDAR" in out
        assert out.count("BEGIN:VEVENT") == 0


class TestAnalyzerCoercion:
    def test_strip_json_fenced(self):
        raw = '```json\n{"action": "SELL"}\n```'
        assert analyzer._strip_json(raw) == '{"action": "SELL"}'

    def test_strip_json_with_preamble(self):
        raw = 'Here is the analysis: {"action":"BUY"} hope it helps'
        assert analyzer._strip_json(raw) == '{"action":"BUY"}'

    def test_coerce_invalid_action_defaults_hold(self):
        rec = analyzer._coerce({"action": "YOLO", "severity": "HIGH"}, "AVEX")
        assert rec["action"] == "HOLD"

    def test_coerce_invalid_severity_defaults_info(self):
        rec = analyzer._coerce({"action": "SELL", "severity": "SUPERBAD"}, "AVEX")
        assert rec["severity"] == "INFO"

    def test_coerce_title_truncated_60(self):
        rec = analyzer._coerce({"title": "x" * 200}, "AVEX")
        assert len(rec["title"]) <= 60

    def test_coerce_fills_missing_price_targets(self):
        rec = analyzer._coerce({"action": "HOLD"}, "AVEX")
        assert set(rec["price_targets"].keys()) == {"30d", "60d", "90d", "eoy"}

    def test_coerce_caps_action_steps(self):
        steps = [{"step": i, "action": "a", "rationale": "r"} for i in range(20)]
        rec = analyzer._coerce({"action_steps": steps}, "AVEX")
        assert len(rec["action_steps"]) <= 5

    def test_hold_fallback_shape(self):
        rec = analyzer._hold_fallback("AVEX", "no key")
        assert rec["action"] == "HOLD"
        assert rec["severity"] == "INFO"
        assert rec["_fallback"] is True
        assert "AVEX" in rec["title"]
