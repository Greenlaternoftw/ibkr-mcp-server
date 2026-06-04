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
            # Pivot-loop persistence. One row per symbol (UNIQUE) for the
            # current loop state; cycles table is the append-only audit
            # trail.  When a loop is stopped, the row stays with
            # status='stopped' so we keep the historical record.
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS pivot_loops (
                    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol                   TEXT NOT NULL UNIQUE,
                    status                   TEXT NOT NULL,
                    initial_capital          REAL NOT NULL,
                    current_capital          REAL NOT NULL,
                    compound                 INTEGER NOT NULL DEFAULT 1,
                    lookback_days            INTEGER NOT NULL,
                    entry_price              REAL,
                    target_price             REAL,
                    stop_price               REAL,
                    current_shares           INTEGER NOT NULL DEFAULT 0,
                    entry_fill_price         REAL,
                    cycle_count              INTEGER NOT NULL DEFAULT 0,
                    win_count                INTEGER NOT NULL DEFAULT 0,
                    loss_count               INTEGER NOT NULL DEFAULT 0,
                    cumulative_realized      REAL NOT NULL DEFAULT 0,
                    catalyst_horizon_days    INTEGER NOT NULL DEFAULT 2,
                    max_drawdown_pct         REAL NOT NULL DEFAULT 50.0,
                    notes                    TEXT,
                    created_at               TEXT NOT NULL,
                    updated_at               TEXT NOT NULL,
                    stopped_at               TEXT,
                    CHECK (status IN ('waiting','entry_pending','holding','exit_pending','paused','stopped'))
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS pivot_loop_cycles (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    loop_id             INTEGER NOT NULL REFERENCES pivot_loops(id) ON DELETE CASCADE,
                    cycle_number        INTEGER NOT NULL,
                    capital_at_start    REAL NOT NULL,
                    entry_price         REAL,
                    entry_fill          REAL,
                    entry_at            TEXT,
                    shares              INTEGER,
                    exit_fill           REAL,
                    exit_at             TEXT,
                    exit_reason         TEXT,
                    realized_pnl        REAL,
                    win                 INTEGER,
                    created_at          TEXT NOT NULL
                )
                """
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_pivot_loop_cycles_loop_id "
                "ON pivot_loop_cycles(loop_id)"
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

    def append_message(
        self,
        thread_id: str,
        role: str,
        content,
    ) -> bool:
        """Append ONE message to a thread without touching the others.

        Used by the IBKR-fills bridge to inject "🟢 FILL ..." synthetic
        messages into the operator's active thread without rewriting the
        whole conversation (which `persist_conversation` does).

        Returns True if appended, False if thread doesn't exist. Bumps
        the thread's updated_at so it sorts to the top of the threads
        list.
        """
        if role not in ("user", "assistant", "system"):
            raise ValueError(f"invalid role: {role}")
        now = _utc_now_iso()
        with self._conn() as c:
            exists = c.execute(
                "SELECT 1 FROM threads WHERE id = ?", (thread_id,)
            ).fetchone()
            if not exists:
                return False
            c.execute(
                "INSERT INTO messages(thread_id, role, content_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (thread_id, role, json.dumps(content, default=str), now),
            )
            c.execute(
                "UPDATE threads SET updated_at = ? WHERE id = ?",
                (now, thread_id),
            )
        return True

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

    # --- pivot loops -------------------------------------------------

    # Allowlist of mutable fields for update_pivot_loop(). Any other key
    # raises -- prevents typos or arbitrary column writes from MCP-tool
    # callers.
    _PIVOT_LOOP_UPDATABLE = {
        "status", "current_capital", "entry_price", "target_price",
        "stop_price", "current_shares", "entry_fill_price",
        "cycle_count", "win_count", "loss_count", "cumulative_realized",
        "catalyst_horizon_days", "max_drawdown_pct", "notes",
    }

    def create_pivot_loop(
        self,
        symbol: str,
        initial_capital: float,
        lookback_days: int,
        *,
        compound: bool = True,
        entry_price: Optional[float] = None,
        target_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        catalyst_horizon_days: int = 2,
        max_drawdown_pct: float = 50.0,
        notes: Optional[str] = None,
    ) -> dict:
        """Start a new pivot loop for `symbol`. Fails (IntegrityError) if
        a loop already exists for that symbol -- caller should stop the
        existing one first via stop_pivot_loop.
        """
        symbol = symbol.upper().strip()
        if not symbol:
            raise ValueError("symbol required")
        if initial_capital < 100:
            raise ValueError("initial_capital must be >= 100")
        if not (3 <= lookback_days <= 180):
            raise ValueError("lookback_days must be 3-180")
        now = _utc_now_iso()
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO pivot_loops (
                    symbol, status, initial_capital, current_capital,
                    compound, lookback_days, entry_price, target_price,
                    stop_price, catalyst_horizon_days, max_drawdown_pct,
                    notes, created_at, updated_at
                ) VALUES (?, 'waiting', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol, initial_capital, initial_capital,
                    1 if compound else 0, lookback_days,
                    entry_price, target_price, stop_price,
                    catalyst_horizon_days, max_drawdown_pct,
                    notes, now, now,
                ),
            )
            return self._row_to_pivot_loop_dict(
                c.execute(
                    "SELECT * FROM pivot_loops WHERE id = ?", (cur.lastrowid,)
                ).fetchone()
            )

    def get_pivot_loop(self, symbol: str) -> Optional[dict]:
        """Fetch the loop for a symbol (any status -- waiting/holding/stopped)."""
        symbol = symbol.upper().strip()
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM pivot_loops WHERE symbol = ?", (symbol,)
            ).fetchone()
        return self._row_to_pivot_loop_dict(row) if row else None

    def list_pivot_loops(self, *, include_stopped: bool = False) -> List[dict]:
        """List all pivot loops, optionally including stopped ones."""
        with self._conn() as c:
            if include_stopped:
                rows = c.execute(
                    "SELECT * FROM pivot_loops ORDER BY updated_at DESC"
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM pivot_loops WHERE status != 'stopped' "
                    "ORDER BY updated_at DESC"
                ).fetchall()
        return [self._row_to_pivot_loop_dict(r) for r in rows]

    def update_pivot_loop(self, symbol: str, **fields) -> Optional[dict]:
        """Update mutable fields of a loop. Returns the updated row.

        Unknown fields raise ValueError to prevent typos sneaking in from
        MCP-tool callers (Claude inventing column names is a real risk).
        """
        symbol = symbol.upper().strip()
        bad = set(fields) - self._PIVOT_LOOP_UPDATABLE
        if bad:
            raise ValueError(
                f"non-updatable fields: {sorted(bad)} "
                f"(allowed: {sorted(self._PIVOT_LOOP_UPDATABLE)})"
            )
        if not fields:
            return self.get_pivot_loop(symbol)
        sets = ", ".join(f"{k} = ?" for k in fields)
        params = list(fields.values()) + [_utc_now_iso(), symbol]
        with self._conn() as c:
            c.execute(
                f"UPDATE pivot_loops SET {sets}, updated_at = ? WHERE symbol = ?",
                params,
            )
            row = c.execute(
                "SELECT * FROM pivot_loops WHERE symbol = ?", (symbol,)
            ).fetchone()
        return self._row_to_pivot_loop_dict(row) if row else None

    def stop_pivot_loop(self, symbol: str) -> Optional[dict]:
        """Mark a loop as stopped. Row + cycle history is preserved."""
        symbol = symbol.upper().strip()
        now = _utc_now_iso()
        with self._conn() as c:
            cur = c.execute(
                "UPDATE pivot_loops SET status = 'stopped', stopped_at = ?, "
                "updated_at = ? WHERE symbol = ? AND status != 'stopped'",
                (now, now, symbol),
            )
            if cur.rowcount == 0:
                return None
            row = c.execute(
                "SELECT * FROM pivot_loops WHERE symbol = ?", (symbol,)
            ).fetchone()
        return self._row_to_pivot_loop_dict(row) if row else None

    def record_pivot_loop_cycle(
        self,
        symbol: str,
        *,
        capital_at_start: float,
        entry_price: Optional[float],
        entry_fill: Optional[float],
        entry_at: Optional[str],
        shares: Optional[int],
        exit_fill: Optional[float],
        exit_at: Optional[str],
        exit_reason: Optional[str],
        realized_pnl: float,
    ) -> dict:
        """Append a completed cycle to the audit trail AND update the
        loop's roll-up counters (cycle_count, win/loss, cumulative_realized,
        current_capital). Atomic in one transaction.
        """
        symbol = symbol.upper().strip()
        now = _utc_now_iso()
        win = 1 if realized_pnl > 0 else 0
        with self._conn() as c:
            loop = c.execute(
                "SELECT * FROM pivot_loops WHERE symbol = ?", (symbol,)
            ).fetchone()
            if loop is None:
                raise ValueError(f"no pivot loop for {symbol}")
            cycle_number = (loop["cycle_count"] or 0) + 1

            c.execute(
                """
                INSERT INTO pivot_loop_cycles (
                    loop_id, cycle_number, capital_at_start, entry_price,
                    entry_fill, entry_at, shares, exit_fill, exit_at,
                    exit_reason, realized_pnl, win, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    loop["id"], cycle_number, capital_at_start, entry_price,
                    entry_fill, entry_at, shares, exit_fill, exit_at,
                    exit_reason, realized_pnl, win, now,
                ),
            )
            new_capital = (
                loop["current_capital"] + realized_pnl
                if loop["compound"] else loop["initial_capital"]
            )
            new_cum = loop["cumulative_realized"] + realized_pnl
            c.execute(
                """
                UPDATE pivot_loops SET
                    cycle_count = ?,
                    win_count = ?,
                    loss_count = ?,
                    cumulative_realized = ?,
                    current_capital = ?,
                    current_shares = 0,
                    entry_fill_price = NULL,
                    status = 'waiting',
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    cycle_number,
                    loop["win_count"] + win,
                    loop["loss_count"] + (1 - win),
                    new_cum,
                    new_capital,
                    now,
                    loop["id"],
                ),
            )
            row = c.execute(
                "SELECT * FROM pivot_loops WHERE id = ?", (loop["id"],)
            ).fetchone()
        out = self._row_to_pivot_loop_dict(row)
        out["last_cycle"] = {
            "cycle_number": cycle_number,
            "realized_pnl": realized_pnl,
            "win": bool(win),
        }
        return out

    def get_pivot_loop_cycles(self, symbol: str, limit: int = 100) -> List[dict]:
        """Return the cycle history for a loop, most recent first."""
        symbol = symbol.upper().strip()
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT c.* FROM pivot_loop_cycles c
                JOIN pivot_loops l ON c.loop_id = l.id
                WHERE l.symbol = ?
                ORDER BY c.cycle_number DESC
                LIMIT ?
                """,
                (symbol, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _row_to_pivot_loop_dict(row) -> Optional[dict]:
        """sqlite3.Row → JSON-friendly dict, with compound as a bool."""
        if row is None:
            return None
        d = dict(row)
        d["compound"] = bool(d.get("compound"))
        return d
