"""SQLite-backed conversation persistence for the chat wrapper.

Why SQLite (not a JSON file, not a managed DB):

  * Single-file, zero-ops, transactional. Backups are ``cp``.
  * Survives daemon restarts; the daemon already creates files in the
    same directory (reversal_state.json, swing_state.json) so the
    operational story is unchanged.
  * The personal-use scale (one user, hundreds of conversations) sits
    comfortably inside SQLite's sweet spot.

Schema (kept deliberately small):

    threads
      id            TEXT PRIMARY KEY     -- thr_<random-hex>
      title         TEXT NOT NULL
      created_at    TEXT NOT NULL        -- ISO 8601 UTC
      updated_at    TEXT NOT NULL        -- ISO 8601 UTC, bumped on each msg

    messages
      id            INTEGER PRIMARY KEY AUTOINCREMENT
      thread_id     TEXT NOT NULL        -- FK to threads.id
      role          TEXT NOT NULL        -- 'user' / 'assistant' / 'tool'
      content_json  TEXT NOT NULL        -- json.dumps of the Anthropic
                                         -- message content (string OR list
                                         -- of blocks, same shape we send
                                         -- back to the API on the next turn)
      created_at    TEXT NOT NULL

The HTTP layer treats threads as opaque IDs and messages as opaque
JSON content; we don't try to parse Anthropic's block shapes here. Keeps
the persistence layer immune to future block-type changes.

Concurrency:
  * SQLite WAL mode is enabled so writes don't block reads.
  * The chat agent runs one turn at a time per browser tab, but the
    daemon could theoretically receive concurrent /chat/api/message
    calls. Each persistence operation opens its own connection and
    commits; the write set per turn is small (1-N messages).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional

logger = logging.getLogger(__name__)


# Schema version baked into a small metadata table so future migrations
# can detect what they're upgrading from. Bump when changing schema.
SCHEMA_VERSION = 1


def _utc_now_iso() -> str:
    """Millisecond-precision ISO 8601. Millisecond rather than second
    so back-to-back operations (e.g. replace_messages on multiple
    threads in the same handler) still have a deterministic ORDER BY
    sort instead of relying on insertion order as a tie-breaker."""
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def _new_thread_id() -> str:
    """Unguessable so cross-thread leakage requires the DB itself, not
    just URL fuzzing. 8 hex bytes = 64 bits of entropy, plenty for
    a personal-scale workspace."""
    return "thr_" + secrets.token_hex(8)


class ChatStore:
    """Thin SQLite wrapper for threads + messages.

    One instance per daemon process. SQLite handles concurrent
    connections internally; we open a fresh connection per operation
    so callers don't share state and tests can use isolated paths.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        # Make sure parent directory exists. The daemon already creates
        # similar state files (reversal_state.json) so the parent should
        # already be writable, but be defensive.
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """One connection per operation. WAL mode is set on init.

        ``PRAGMA foreign_keys`` is a per-CONNECTION setting in SQLite
        (it doesn't persist with the database file), so it has to be
        re-enabled here, not just in _init_schema. Without this,
        ON DELETE CASCADE silently does nothing.
        """
        c = sqlite3.connect(self.db_path, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        try:
            yield c
        finally:
            c.close()

    def _init_schema(self) -> None:
        with self._conn() as c:
            # WAL: readers never block writers, writers never block readers.
            # Standard recommendation for any SQLite used by a long-lived
            # process; one-time setup, persists in the DB file.
            c.execute("PRAGMA journal_mode = WAL")
            c.execute("PRAGMA foreign_keys = ON")
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS threads (
                    id          TEXT PRIMARY KEY,
                    title       TEXT NOT NULL,
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id    TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
                    role         TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    created_at   TEXT NOT NULL
                )
                """
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_thread "
                "ON messages(thread_id, id)"
            )
            # Mutable auth config (PIN). Stored here rather than in .env
            # so it can be rotated from the UI without an SSH session +
            # daemon restart. Plaintext PIN -- same security floor as
            # the bearer token in .env; rate-limiter is what makes the
            # short PIN brute-force-safe.
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_config (
                    key         TEXT PRIMARY KEY,
                    value       TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                )
                """
            )
            # Portfolio equity snapshots for the equity-curve chart tool.
            # One row per snapshot interval; account scoping lets us
            # support paper + live accounts side by side later. We keep
            # the values denormalized (cash + positions_value alongside
            # the top-line net_liquidation) so the chart can also show
            # the cash/positions split if we want it.
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp        TEXT NOT NULL,
                    account          TEXT NOT NULL,
                    net_liquidation  REAL NOT NULL,
                    total_cash       REAL,
                    positions_value  REAL,
                    buying_power     REAL
                )
                """
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_snapshots_account_ts "
                "ON portfolio_snapshots(account, timestamp)"
            )
            # Command-Center: user-defined watchlist "portfolios" (multiple
            # named groups of tickers). Independent of IBKR positions --
            # these are for tracking + research, NOT for orders.
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS watchlists (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    name        TEXT NOT NULL UNIQUE,
                    created_at  TEXT NOT NULL,
                    sort_order  INTEGER DEFAULT 0
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS watchlist_stocks (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    watchlist_id  INTEGER NOT NULL REFERENCES watchlists(id) ON DELETE CASCADE,
                    symbol        TEXT NOT NULL,
                    rating        TEXT,
                    current_price REAL,
                    target_price  REAL,
                    range_low     REAL,
                    range_high    REAL,
                    notes         TEXT,
                    added_at      TEXT NOT NULL,
                    updated_at    TEXT NOT NULL,
                    UNIQUE(watchlist_id, symbol)
                )
                """
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_wstocks_watchlist "
                "ON watchlist_stocks(watchlist_id)"
            )
            # Generic key-value preferences -- UI state that used to
            # live in browser localStorage. Moving it server-side means
            # one device's settings (active watchlist tab, gate
            # threshold, view mode, etc.) sync to every other device.
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS user_prefs (
                    key         TEXT PRIMARY KEY,
                    value       TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                )
                """
            )
            c.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    # --- threads --------------------------------------------------------

    def create_thread(self, title: str) -> dict:
        tid = _new_thread_id()
        now = _utc_now_iso()
        with self._conn() as c:
            c.execute(
                "INSERT INTO threads(id, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (tid, title or "New chat", now, now),
            )
        return {
            "id": tid,
            "title": title or "New chat",
            "created_at": now,
            "updated_at": now,
            "message_count": 0,
        }

    def list_threads(self, limit: int = 100) -> List[dict]:
        """Most-recently-updated first."""
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT
                    t.id, t.title, t.created_at, t.updated_at,
                    (SELECT COUNT(*) FROM messages m WHERE m.thread_id = t.id)
                        AS message_count
                FROM threads t
                ORDER BY t.updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_thread(self, thread_id: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT id, title, created_at, updated_at FROM threads WHERE id = ?",
                (thread_id,),
            ).fetchone()
        return dict(row) if row else None

    def rename_thread(self, thread_id: str, new_title: str) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE threads SET title = ?, updated_at = ? WHERE id = ?",
                (new_title, _utc_now_iso(), thread_id),
            )
            return cur.rowcount > 0

    def delete_thread(self, thread_id: str) -> bool:
        """Hard delete -- ON DELETE CASCADE wipes messages too."""
        with self._conn() as c:
            cur = c.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
            return cur.rowcount > 0

    # --- messages -------------------------------------------------------

    def get_messages(self, thread_id: str) -> List[dict]:
        """Return messages in insert order as Anthropic-API-ready dicts.

        Each row's content_json was already in the shape Anthropic wants
        (string OR list of blocks) when persisted, so callers can pass
        the result straight back into messages.create() with no extra
        coercion.
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT role, content_json FROM messages "
                "WHERE thread_id = ? ORDER BY id ASC",
                (thread_id,),
            ).fetchall()
        return [
            {"role": r["role"], "content": json.loads(r["content_json"])}
            for r in rows
        ]

    # --- auth config (PIN) ---------------------------------------------

    def get_pin(self) -> Optional[str]:
        """Return the currently active PIN, or None if none is set."""
        with self._conn() as c:
            row = c.execute(
                "SELECT value FROM auth_config WHERE key = 'pin'"
            ).fetchone()
        return row["value"] if row else None

    def set_pin(self, pin: str) -> None:
        """Insert or overwrite the active PIN."""
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO auth_config(key, value, updated_at) "
                "VALUES('pin', ?, ?)",
                (pin, _utc_now_iso()),
            )

    def clear_pin(self) -> None:
        """Remove the stored PIN (forces fallback to env-var)."""
        with self._conn() as c:
            c.execute("DELETE FROM auth_config WHERE key = 'pin'")

    # --- portfolio snapshots -------------------------------------------

    def record_snapshot(
        self,
        *,
        account: str,
        net_liquidation: float,
        total_cash: Optional[float] = None,
        positions_value: Optional[float] = None,
        buying_power: Optional[float] = None,
    ) -> None:
        """Record one equity snapshot. Caller decides cadence (typically
        hourly via the background snapshot task)."""
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO portfolio_snapshots(
                    timestamp, account, net_liquidation,
                    total_cash, positions_value, buying_power
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    _utc_now_iso(),
                    account,
                    float(net_liquidation),
                    None if total_cash is None else float(total_cash),
                    None if positions_value is None else float(positions_value),
                    None if buying_power is None else float(buying_power),
                ),
            )

    def get_snapshots(
        self,
        *,
        account: str,
        lookback_days: Optional[int] = None,
    ) -> List[dict]:
        """Oldest-first list of snapshots for one account.

        ``lookback_days`` filters to the most-recent N days; None returns
        the full history. Chart tool uses lookback to keep the X axis
        readable on long-running accounts.
        """
        query = (
            "SELECT timestamp, net_liquidation, total_cash, "
            "positions_value, buying_power "
            "FROM portfolio_snapshots WHERE account = ? "
        )
        params: list = [account]
        if lookback_days:
            from datetime import datetime, timedelta, timezone
            cutoff = (
                datetime.now(timezone.utc) - timedelta(days=lookback_days)
            ).isoformat(timespec="milliseconds")
            query += "AND timestamp >= ? "
            params.append(cutoff)
        query += "ORDER BY timestamp ASC"
        with self._conn() as c:
            rows = c.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # --- watchlists (Command-Center portfolio tabs) -------------------

    def list_watchlists(self) -> List[dict]:
        """All watchlists with their stock counts. Sort_order then name."""
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT w.id, w.name, w.created_at, w.sort_order,
                  (SELECT COUNT(*) FROM watchlist_stocks s WHERE s.watchlist_id = w.id) AS stock_count
                FROM watchlists w
                ORDER BY w.sort_order, w.name
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def create_watchlist(self, name: str) -> dict:
        """Create a new (empty) watchlist. Raises sqlite3.IntegrityError
        if the name is already taken."""
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO watchlists(name, created_at, sort_order) "
                "VALUES (?, ?, COALESCE((SELECT MAX(sort_order) FROM watchlists), 0) + 1)",
                (name, _utc_now_iso()),
            )
            new_id = cur.lastrowid
            row = c.execute(
                "SELECT id, name, created_at, sort_order FROM watchlists WHERE id = ?",
                (new_id,),
            ).fetchone()
        d = dict(row)
        d["stock_count"] = 0
        return d

    def delete_watchlist(self, watchlist_id: int) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM watchlists WHERE id = ?", (watchlist_id,))
            return cur.rowcount > 0

    def get_watchlist_stocks(self, watchlist_id: int) -> List[dict]:
        """All stocks in one watchlist, in insertion order."""
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT id, symbol, rating, current_price, target_price,
                       range_low, range_high, notes, added_at, updated_at
                FROM watchlist_stocks WHERE watchlist_id = ?
                ORDER BY id ASC
                """,
                (watchlist_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def add_watchlist_stock(self, watchlist_id: int, symbol: str) -> dict:
        """Insert a placeholder row for a symbol (metrics filled later
        via upsert_watchlist_stock). Raises IntegrityError on duplicate."""
        now = _utc_now_iso()
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO watchlist_stocks(watchlist_id, symbol, added_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (watchlist_id, symbol.upper(), now, now),
            )
            row = c.execute(
                "SELECT id, symbol, rating, current_price, target_price, "
                "range_low, range_high, notes, added_at, updated_at "
                "FROM watchlist_stocks WHERE id = ?",
                (cur.lastrowid,),
            ).fetchone()
        return dict(row)

    def upsert_watchlist_stock(
        self,
        watchlist_id: int,
        symbol: str,
        *,
        rating: Optional[str] = None,
        current_price: Optional[float] = None,
        target_price: Optional[float] = None,
        range_low: Optional[float] = None,
        range_high: Optional[float] = None,
        notes: Optional[str] = None,
    ) -> Optional[dict]:
        """Update an existing watchlist row's metrics (rating, prices, etc).
        Only non-None fields are written -- partial updates are common
        when only the price refresh fires. Returns the updated row, or
        None if the (watchlist_id, symbol) pair doesn't exist."""
        sets, params = [], []
        if rating is not None:
            sets.append("rating = ?"); params.append(rating)
        if current_price is not None:
            sets.append("current_price = ?"); params.append(float(current_price))
        if target_price is not None:
            sets.append("target_price = ?"); params.append(float(target_price))
        if range_low is not None:
            sets.append("range_low = ?"); params.append(float(range_low))
        if range_high is not None:
            sets.append("range_high = ?"); params.append(float(range_high))
        if notes is not None:
            sets.append("notes = ?"); params.append(notes)
        if not sets:
            # Nothing to update, just bump timestamp.
            sets.append("updated_at = ?"); params.append(_utc_now_iso())
        else:
            sets.append("updated_at = ?"); params.append(_utc_now_iso())
        params.extend([watchlist_id, symbol.upper()])
        with self._conn() as c:
            cur = c.execute(
                "UPDATE watchlist_stocks SET " + ", ".join(sets) +
                " WHERE watchlist_id = ? AND symbol = ?",
                params,
            )
            if cur.rowcount == 0:
                return None
            row = c.execute(
                "SELECT id, symbol, rating, current_price, target_price, "
                "range_low, range_high, notes, added_at, updated_at "
                "FROM watchlist_stocks WHERE watchlist_id = ? AND symbol = ?",
                (watchlist_id, symbol.upper()),
            ).fetchone()
        return dict(row) if row else None

    def remove_watchlist_stock(self, watchlist_id: int, symbol: str) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM watchlist_stocks "
                "WHERE watchlist_id = ? AND symbol = ?",
                (watchlist_id, symbol.upper()),
            )
            return cur.rowcount > 0

    # --- generic user preferences (UI state) ---------------------------

    def get_pref(self, key: str) -> Optional[str]:
        """Returns the raw string value, or None if unset.

        The UI is responsible for JSON-encoding/decoding any structured
        values it stores. Keeping the column TEXT means the persistence
        layer doesn't have to grow alongside whatever UI state shows up.
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT value FROM user_prefs WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def set_pref(self, key: str, value: str) -> None:
        """Upsert a single preference. Limit key/value size to keep one
        misbehaving caller from filling the DB."""
        if len(key) > 256:
            raise ValueError("pref key too long (max 256)")
        if len(value) > 65536:
            raise ValueError("pref value too long (max 64KB)")
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO user_prefs(key, value, updated_at) "
                "VALUES (?, ?, ?)",
                (key, value, _utc_now_iso()),
            )

    def delete_pref(self, key: str) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM user_prefs WHERE key = ?", (key,))
            return cur.rowcount > 0

    def list_prefs(self) -> dict:
        """All prefs as a {key: value} dict. Used for bulk-load on
        page boot to avoid a round-trip per pref."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT key, value FROM user_prefs"
            ).fetchall()
        return {r["key"]: r["value"] for r in rows}

    def snapshot_count(self, account: Optional[str] = None) -> int:
        """For health / debug. Total snapshots, optionally per-account."""
        with self._conn() as c:
            if account:
                row = c.execute(
                    "SELECT COUNT(*) AS n FROM portfolio_snapshots WHERE account = ?",
                    (account,),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT COUNT(*) AS n FROM portfolio_snapshots"
                ).fetchone()
        return int(row["n"])

    # --- messages -------------------------------------------------------

    def replace_messages(self, thread_id: str, conversation: List[dict]) -> None:
        """Overwrite a thread's messages with the given conversation.

        Called after each turn so the persisted state matches what the
        agent loop produced (which may include intermediate tool_use /
        tool_result blocks the client didn't send). Simpler than diffing
        and avoids "phantom" messages if anything goes wrong mid-turn.

        Always bumps updated_at so the thread floats to the top of the
        list view.
        """
        now = _utc_now_iso()
        with self._conn() as c:
            c.execute("BEGIN")
            try:
                c.execute("DELETE FROM messages WHERE thread_id = ?", (thread_id,))
                for msg in conversation:
                    c.execute(
                        "INSERT INTO messages(thread_id, role, content_json, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            thread_id,
                            msg["role"],
                            json.dumps(msg.get("content"), default=str),
                            now,
                        ),
                    )
                c.execute(
                    "UPDATE threads SET updated_at = ? WHERE id = ?",
                    (now, thread_id),
                )
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
