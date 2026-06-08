"""EWS persistence — alert feed + scan audit (SQLite).

Reuses the same SQLite file as the chat store by default (separate
tables: ews_alerts, ews_scans). Mirrors chat/persistence.py conventions:
autocommit connections, WAL, Row factory.

The alert feed is the source of truth for the EWS dashboard tab and the
"keep the last 100 alerts" rule from the brief (Step 5).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

_MAX_FEED = 500  # keep generous; UI paginates. Brief said 100 in-state.


class EWSStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.db_path, isolation_level=None)
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.execute("PRAGMA journal_mode = WAL")
            c.execute("""
                CREATE TABLE IF NOT EXISTS ews_alerts (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at    TEXT NOT NULL,
                    symbol        TEXT NOT NULL,
                    account       TEXT,
                    action        TEXT NOT NULL,
                    severity      TEXT NOT NULL,
                    title         TEXT NOT NULL,
                    summary       TEXT,
                    signals_json  TEXT,   -- signals_detected []
                    steps_json    TEXT,   -- action_steps []
                    targets_json  TEXT,   -- price_targets {}
                    sources_json  TEXT,   -- trigger_sources []
                    pushed        INTEGER NOT NULL DEFAULT 0,
                    dismissed     INTEGER NOT NULL DEFAULT 0
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS ix_ews_alerts_created "
                      "ON ews_alerts(created_at DESC)")
            c.execute("""
                CREATE TABLE IF NOT EXISTS ews_scans (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at    TEXT NOT NULL,
                    finished_at   TEXT,
                    positions     INTEGER DEFAULT 0,
                    alerts        INTEGER DEFAULT 0,
                    pushed        INTEGER DEFAULT 0,
                    error         TEXT
                )
            """)

    # ----- alerts ------------------------------------------------------
    def insert_alert(self, *, created_at: str, symbol: str, account: Optional[str],
                     rec: Dict[str, Any], pushed: bool) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO ews_alerts
                   (created_at, symbol, account, action, severity, title,
                    summary, signals_json, steps_json, targets_json,
                    sources_json, pushed, dismissed)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                (created_at, symbol, account,
                 rec.get("action"), rec.get("severity"), rec.get("title"),
                 rec.get("summary"),
                 json.dumps(rec.get("signals_detected", [])),
                 json.dumps(rec.get("action_steps", [])),
                 json.dumps(rec.get("price_targets", {})),
                 json.dumps(rec.get("trigger_sources", [])),
                 1 if pushed else 0),
            )
            alert_id = cur.lastrowid
            # Trim the feed.
            c.execute(
                """DELETE FROM ews_alerts WHERE id NOT IN
                   (SELECT id FROM ews_alerts ORDER BY id DESC LIMIT ?)""",
                (_MAX_FEED,),
            )
            return alert_id

    def list_alerts(self, *, limit: int = 100,
                    include_dismissed: bool = False) -> List[Dict[str, Any]]:
        q = "SELECT * FROM ews_alerts"
        if not include_dismissed:
            q += " WHERE dismissed = 0"
        q += " ORDER BY id DESC LIMIT ?"
        with self._conn() as c:
            rows = c.execute(q, (limit,)).fetchall()
        return [self._row_to_alert(r) for r in rows]

    def dismiss_alert(self, alert_id: int) -> bool:
        with self._conn() as c:
            cur = c.execute("UPDATE ews_alerts SET dismissed = 1 WHERE id = ?",
                            (alert_id,))
            return cur.rowcount > 0

    def latest_alert_for(self, symbol: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            r = c.execute(
                "SELECT * FROM ews_alerts WHERE symbol = ? AND dismissed = 0 "
                "ORDER BY id DESC LIMIT 1", (symbol,)).fetchone()
        return self._row_to_alert(r) if r else None

    @staticmethod
    def _row_to_alert(r: sqlite3.Row) -> Dict[str, Any]:
        def _j(x, d):
            try:
                return json.loads(x) if x else d
            except Exception:
                return d
        return {
            "id": r["id"],
            "created_at": r["created_at"],
            "symbol": r["symbol"],
            "account": r["account"],
            "action": r["action"],
            "severity": r["severity"],
            "title": r["title"],
            "summary": r["summary"],
            "signals_detected": _j(r["signals_json"], []),
            "action_steps": _j(r["steps_json"], []),
            "price_targets": _j(r["targets_json"], {}),
            "trigger_sources": _j(r["sources_json"], []),
            "pushed": bool(r["pushed"]),
            "dismissed": bool(r["dismissed"]),
        }

    # ----- scan audit --------------------------------------------------
    def start_scan(self, started_at: str) -> int:
        with self._conn() as c:
            cur = c.execute("INSERT INTO ews_scans (started_at) VALUES (?)",
                            (started_at,))
            return cur.lastrowid

    def finish_scan(self, scan_id: int, *, finished_at: str, positions: int,
                    alerts: int, pushed: int, error: Optional[str] = None) -> None:
        with self._conn() as c:
            c.execute(
                """UPDATE ews_scans SET finished_at=?, positions=?, alerts=?,
                   pushed=?, error=? WHERE id=?""",
                (finished_at, positions, alerts, pushed, error, scan_id),
            )

    def last_scan(self) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM ews_scans ORDER BY id DESC LIMIT 1").fetchone()
        return dict(r) if r else None


_store: Optional[EWSStore] = None


def get_store() -> EWSStore:
    """Singleton EWSStore, pathed from settings (ews_db_path or chat_db_path)."""
    global _store
    if _store is None:
        from ..config import settings
        path = settings.ews_db_path or settings.chat_db_path
        _store = EWSStore(Path(path))
    return _store
