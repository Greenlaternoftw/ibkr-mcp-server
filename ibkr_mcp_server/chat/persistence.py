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
