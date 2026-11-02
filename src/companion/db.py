"""SQLite connection and schema shared by all stores.

The schema is idempotent (CREATE ... IF NOT EXISTS), so it is applied on every
start. A single aiosqlite connection serializes access, which is plenty for a
private bot.
"""

from pathlib import Path

import aiosqlite

SCHEMA = """
-- Chat mode: bot-directed exchanges per channel/thread.
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_id   TEXT    NOT NULL,
    role       TEXT    NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_messages_scope ON messages (scope_id, id);

-- Game Master mode: one game per Discord thread.
CREATE TABLE IF NOT EXISTS games (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         TEXT    NOT NULL,
    thread_id        TEXT    NOT NULL UNIQUE,
    host_id          TEXT    NOT NULL,
    status           TEXT    NOT NULL,
    genre            TEXT    NOT NULL DEFAULT '',
    title            TEXT    NOT NULL DEFAULT '',
    pitches_json     TEXT    NOT NULL DEFAULT '[]',
    outline_json     TEXT    NOT NULL DEFAULT '{}',
    summary          TEXT    NOT NULL DEFAULT '',
    act              INTEGER NOT NULL DEFAULT 1,
    round_number     INTEGER NOT NULL DEFAULT 0,
    round_deadline   REAL,
    nudged           INTEGER NOT NULL DEFAULT 0,
    resolve_failures INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS players (
    game_id        INTEGER NOT NULL REFERENCES games (id) ON DELETE CASCADE,
    user_id        TEXT    NOT NULL,
    display_name   TEXT    NOT NULL,
    concept        TEXT    NOT NULL DEFAULT '',
    character_json TEXT,
    joined_round   INTEGER NOT NULL DEFAULT 0,
    is_ai          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (game_id, user_id)
);

-- In-character posts (thread chat and /act notes), per round.
CREATE TABLE IF NOT EXISTS round_posts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id      INTEGER NOT NULL REFERENCES games (id) ON DELETE CASCADE,
    round_number INTEGER NOT NULL,
    user_id      TEXT    NOT NULL,
    content      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_round_posts ON round_posts (game_id, round_number);

-- Who has used /act in a round.
CREATE TABLE IF NOT EXISTS round_actions (
    game_id      INTEGER NOT NULL REFERENCES games (id) ON DELETE CASCADE,
    round_number INTEGER NOT NULL,
    user_id      TEXT    NOT NULL,
    PRIMARY KEY (game_id, round_number, user_id)
);

-- Story bible: established facts, upserted by (kind, name).
CREATE TABLE IF NOT EXISTS canon (
    game_id INTEGER NOT NULL REFERENCES games (id) ON DELETE CASCADE,
    kind    TEXT    NOT NULL,
    name    TEXT    NOT NULL,
    detail  TEXT    NOT NULL,
    PRIMARY KEY (game_id, kind, name)
);

-- GM narration posted each round (round 0 is the opening scene).
CREATE TABLE IF NOT EXISTS narrations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id      INTEGER NOT NULL REFERENCES games (id) ON DELETE CASCADE,
    round_number INTEGER NOT NULL,
    content      TEXT    NOT NULL
);

-- Cross-process coordination. This is effective when all instances use the
-- same database file (for example a shared mounted volume).
CREATE TABLE IF NOT EXISTS processed_events (
    event_id   TEXT PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS coordination_locks (
    lock_name  TEXT PRIMARY KEY,
    owner_id   TEXT NOT NULL,
    expires_at REAL NOT NULL
);
"""


async def connect(path: Path | str) -> aiosqlite.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(path)
    await db.execute("PRAGMA foreign_keys = ON")
    await db.execute("PRAGMA busy_timeout = 10000")
    if str(path) != ":memory:":
        await db.execute("PRAGMA journal_mode = WAL")
    await db.executescript(SCHEMA)
    async with db.execute("PRAGMA table_info(players)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    if "is_ai" not in columns:
        await db.execute("ALTER TABLE players ADD COLUMN is_ai INTEGER NOT NULL DEFAULT 0")
    await db.commit()
    return db
