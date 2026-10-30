"""SQLite-backed conversation context, keyed by scope (a channel or thread ID).

Only bot-directed exchanges are stored: the user's prompt and the bot's reply.
"""

from pathlib import Path

import aiosqlite

from companion.llm import ChatMessage

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_id   TEXT    NOT NULL,
    role       TEXT    NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_messages_scope ON messages (scope_id, id);
"""


class ContextStore:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    @classmethod
    async def open(cls, path: Path | str) -> "ContextStore":
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(path)
        await db.executescript(_SCHEMA)
        await db.commit()
        return cls(db)

    async def close(self) -> None:
        await self._db.close()

    async def add_exchange(
        self, scope_id: str, user_content: str, assistant_content: str, retain: int
    ) -> None:
        """Store a user/assistant pair, then prune the scope to the newest `retain` messages."""
        await self._db.executemany(
            "INSERT INTO messages (scope_id, role, content) VALUES (?, ?, ?)",
            [(scope_id, "user", user_content), (scope_id, "assistant", assistant_content)],
        )
        await self._db.execute(
            """
            DELETE FROM messages WHERE scope_id = ? AND id NOT IN (
                SELECT id FROM messages WHERE scope_id = ? ORDER BY id DESC LIMIT ?
            )
            """,
            (scope_id, scope_id, max(retain, 0)),
        )
        await self._db.commit()

    async def recent(self, scope_id: str, limit: int) -> list[ChatMessage]:
        """Return up to `limit` most recent messages for a scope, oldest first."""
        if limit <= 0:
            return []
        async with self._db.execute(
            "SELECT role, content FROM messages WHERE scope_id = ? ORDER BY id DESC LIMIT ?",
            (scope_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [ChatMessage(role, content) for role, content in reversed(rows)]
