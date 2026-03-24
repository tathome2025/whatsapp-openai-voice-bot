from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings


class AppRepo:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db_path = Path(settings.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS admin_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL DEFAULT '',
                    password_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_login_at TEXT
                );

                CREATE TABLE IF NOT EXISTS whitelist_contacts (
                    chat_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversation_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    role TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    message_text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_conversation_chat_created
                ON conversation_logs (chat_id, created_at DESC, id DESC);

                CREATE TABLE IF NOT EXISTS user_memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_memories_chat_status
                ON user_memories (chat_id, status, created_at DESC, id DESC);
                """
            )

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def is_whitelisted(self, chat_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM whitelist_contacts WHERE chat_id = ?", (chat_id,)).fetchone()
            return row is not None

    def list_whitelist(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT chat_id, label, created_at, updated_at FROM whitelist_contacts ORDER BY updated_at DESC, chat_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_whitelist(self, chat_id: str, label: str = "") -> dict[str, Any]:
        now = self._now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO whitelist_contacts (chat_id, label, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    label = excluded.label,
                    updated_at = excluded.updated_at
                """,
                (chat_id, label, now, now),
            )
            row = conn.execute(
                "SELECT chat_id, label, created_at, updated_at FROM whitelist_contacts WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        return dict(row) if row else {}

    def remove_whitelist(self, chat_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM whitelist_contacts WHERE chat_id = ?", (chat_id,))
        return cur.rowcount > 0

    def log_message(self, chat_id: str, *, direction: str, role: str, source_type: str, message_text: str) -> None:
        now = self._now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversation_logs (chat_id, direction, role, source_type, message_text, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (chat_id, direction, role, source_type, message_text, now),
            )

    def list_conversation_logs(self, chat_id: str, limit: int = 200) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 1000))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, chat_id, direction, role, source_type, message_text, created_at
                FROM conversation_logs
                WHERE chat_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (chat_id, safe_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_known_users(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                WITH users AS (
                    SELECT chat_id FROM whitelist_contacts
                    UNION
                    SELECT chat_id FROM conversation_logs
                    UNION
                    SELECT chat_id FROM user_memories
                )
                SELECT
                    u.chat_id AS chat_id,
                    COALESCE(w.label, '') AS label,
                    (
                        SELECT message_text FROM conversation_logs c
                        WHERE c.chat_id = u.chat_id
                        ORDER BY c.id DESC LIMIT 1
                    ) AS last_message,
                    (
                        SELECT created_at FROM conversation_logs c
                        WHERE c.chat_id = u.chat_id
                        ORDER BY c.id DESC LIMIT 1
                    ) AS last_message_at,
                    EXISTS(SELECT 1 FROM whitelist_contacts wx WHERE wx.chat_id = u.chat_id) AS whitelisted
                FROM users u
                LEFT JOIN whitelist_contacts w ON w.chat_id = u.chat_id
                ORDER BY COALESCE(last_message_at, '') DESC, u.chat_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def add_memory(self, chat_id: str, content: str, *, created_by: str) -> dict[str, Any]:
        now = self._now_iso()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO user_memories (chat_id, content, created_by, status, created_at, updated_at)
                VALUES (?, ?, ?, 'active', ?, ?)
                """,
                (chat_id, content, created_by, now, now),
            )
            row = conn.execute(
                "SELECT id, chat_id, content, created_by, status, created_at, updated_at FROM user_memories WHERE id = ?",
                (cur.lastrowid,),
            ).fetchone()
        return dict(row) if row else {}

    def list_memories(self, chat_id: str, *, include_inactive: bool = False) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if include_inactive:
                rows = conn.execute(
                    """
                    SELECT id, chat_id, content, created_by, status, created_at, updated_at
                    FROM user_memories
                    WHERE chat_id = ?
                    ORDER BY id DESC
                    """,
                    (chat_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, chat_id, content, created_by, status, created_at, updated_at
                    FROM user_memories
                    WHERE chat_id = ? AND status = 'active'
                    ORDER BY id DESC
                    """,
                    (chat_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    def archive_memory(self, memory_id: int) -> bool:
        now = self._now_iso()
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE user_memories SET status = 'archived', updated_at = ? WHERE id = ?",
                (now, int(memory_id)),
            )
        return cur.rowcount > 0

    def get_admin_user_by_email(self, email: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, email, display_name, password_hash, status, created_at, updated_at, last_login_at FROM admin_users WHERE lower(email)=lower(?)",
                (email,),
            ).fetchone()
        return dict(row) if row else None

    def get_admin_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, email, display_name, password_hash, status, created_at, updated_at, last_login_at FROM admin_users WHERE id = ?",
                (int(user_id),),
            ).fetchone()
        return dict(row) if row else None

    def list_admin_users(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, email, display_name, status, created_at, updated_at, last_login_at FROM admin_users ORDER BY id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_admin_user(self, *, email: str, display_name: str, password_hash: str, status: str = "active") -> dict[str, Any]:
        now = self._now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO admin_users (email, display_name, password_hash, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    display_name = excluded.display_name,
                    password_hash = excluded.password_hash,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (email.strip().lower(), display_name.strip(), password_hash, status, now, now),
            )
            row = conn.execute(
                "SELECT id, email, display_name, status, created_at, updated_at, last_login_at FROM admin_users WHERE lower(email)=lower(?)",
                (email,),
            ).fetchone()
        return dict(row) if row else {}

    def touch_admin_login(self, user_id: int) -> None:
        now = self._now_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE admin_users SET last_login_at = ?, updated_at = ? WHERE id = ?",
                (now, now, int(user_id)),
            )

    def count_admin_users(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(1) AS cnt FROM admin_users").fetchone()
        return int(row["cnt"] if row else 0)
