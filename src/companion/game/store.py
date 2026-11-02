"""Persistence for games, players, rounds, and the story bible."""

import json
from dataclasses import dataclass, field
from enum import StrEnum

import aiosqlite

from companion.game.schemas import CanonEntry, Character


class Status(StrEnum):
    PITCHING = "pitching"  # waiting for the host to pick one of the GM's premises
    LOBBY = "lobby"  # players joining; host hasn't begun
    ACTIVE = "active"
    ENDED = "ended"


@dataclass
class Game:
    id: int
    guild_id: str
    thread_id: str
    host_id: str
    status: Status
    genre: str = ""
    title: str = ""
    pitches: list[dict] = field(default_factory=list)
    outline: dict = field(default_factory=dict)
    summary: str = ""
    act: int = 1
    round_number: int = 0
    round_deadline: float | None = None
    nudged: bool = False
    resolve_failures: int = 0


@dataclass
class Player:
    user_id: str
    display_name: str
    concept: str = ""
    character: Character | None = None
    joined_round: int = 0
    is_ai: bool = False

    @property
    def label(self) -> str:
        """e.g. "Sam (Vex)" once a character exists, else just "Sam"."""
        if self.character:
            return f"{self.display_name} ({self.character.name})"
        return self.display_name


_GAME_COLUMNS = (
    "id, guild_id, thread_id, host_id, status, genre, title, pitches_json, outline_json, "
    "summary, act, round_number, round_deadline, nudged, resolve_failures"
)
_UPDATABLE = {
    "status",
    "genre",
    "title",
    "pitches",
    "outline",
    "summary",
    "act",
    "round_number",
    "round_deadline",
    "nudged",
    "resolve_failures",
}
_JSON_FIELDS = {"pitches": "pitches_json", "outline": "outline_json"}
# Only constant column names are interpolated into SQL below; values are always bound.
_SELECT_GAMES = f"SELECT {_GAME_COLUMNS} FROM games"  # noqa: S608


def _game_from_row(row) -> Game:
    return Game(
        id=row[0],
        guild_id=row[1],
        thread_id=row[2],
        host_id=row[3],
        status=Status(row[4]),
        genre=row[5],
        title=row[6],
        pitches=json.loads(row[7]),
        outline=json.loads(row[8]),
        summary=row[9],
        act=row[10],
        round_number=row[11],
        round_deadline=row[12],
        nudged=bool(row[13]),
        resolve_failures=row[14],
    )


class GameStore:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    # --- games ---------------------------------------------------------------

    async def create_game(
        self, guild_id: str, thread_id: str, host_id: str, status: Status, genre: str = ""
    ) -> Game:
        cursor = await self._db.execute(
            "INSERT INTO games (guild_id, thread_id, host_id, status, genre) "
            "VALUES (?, ?, ?, ?, ?)",
            (guild_id, thread_id, host_id, status.value, genre),
        )
        await self._db.commit()
        game = await self.get_game(cursor.lastrowid)
        if game is None:
            raise RuntimeError("game vanished immediately after insert")
        return game

    async def get_game(self, game_id: int) -> Game | None:
        return await self._one(f"{_SELECT_GAMES} WHERE id = ?", (game_id,))

    async def game_for_thread(self, thread_id: str) -> Game | None:
        return await self._one(f"{_SELECT_GAMES} WHERE thread_id = ?", (thread_id,))

    async def active_games(self) -> list[Game]:
        async with self._db.execute(
            f"{_SELECT_GAMES} WHERE status = ? ORDER BY id",
            (Status.ACTIVE.value,),
        ) as cursor:
            return [_game_from_row(row) for row in await cursor.fetchall()]

    async def update_game(self, game_id: int, **fields) -> None:
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise ValueError(f"cannot update game fields: {sorted(unknown)}")
        columns, values = [], []
        for name, value in fields.items():
            if name in _JSON_FIELDS:
                columns.append(_JSON_FIELDS[name])
                values.append(json.dumps(value))
            else:
                columns.append(name)
                values.append(value.value if isinstance(value, Status) else value)
        assignments = ", ".join(f"{column} = ?" for column in columns)
        sql = f"UPDATE games SET {assignments} WHERE id = ?"  # noqa: S608 - allowlisted columns
        await self._db.execute(sql, (*values, game_id))
        await self._db.commit()

    async def _one(self, sql: str, params: tuple) -> Game | None:
        async with self._db.execute(sql, params) as cursor:
            row = await cursor.fetchone()
        return _game_from_row(row) if row else None

    # --- players ---------------------------------------------------------------

    async def add_player(
        self,
        game_id: int,
        user_id: str,
        display_name: str,
        concept: str,
        joined_round: int,
        *,
        is_ai: bool = False,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO players (game_id, user_id, display_name, concept, joined_round, is_ai)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (game_id, user_id) DO UPDATE
                SET display_name = excluded.display_name, concept = excluded.concept,
                    is_ai = excluded.is_ai
            """,
            (game_id, user_id, display_name, concept, joined_round, int(is_ai)),
        )
        await self._db.commit()

    async def set_character(self, game_id: int, user_id: str, character: Character) -> None:
        await self._db.execute(
            "UPDATE players SET character_json = ? WHERE game_id = ? AND user_id = ?",
            (character.model_dump_json(), game_id, user_id),
        )
        await self._db.commit()

    async def players(self, game_id: int) -> list[Player]:
        async with self._db.execute(
            """
            SELECT user_id, display_name, concept, character_json, joined_round, is_ai
            FROM players WHERE game_id = ? ORDER BY rowid
            """,
            (game_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            Player(
                user_id=row[0],
                display_name=row[1],
                concept=row[2],
                character=Character.model_validate_json(row[3]) if row[3] else None,
                joined_round=row[4],
                is_ai=bool(row[5]),
            )
            for row in rows
        ]

    async def player_for_game(self, game_id: int, user_id: str) -> Player | None:
        players = await self.players(game_id)
        return next((player for player in players if player.user_id == user_id), None)

    # --- rounds ------------------------------------------------------------------

    async def add_post(self, game_id: int, round_number: int, user_id: str, content: str) -> None:
        await self._db.execute(
            "INSERT INTO round_posts (game_id, round_number, user_id, content) VALUES (?, ?, ?, ?)",
            (game_id, round_number, user_id, content),
        )
        await self._db.commit()

    async def posts(self, game_id: int, round_number: int) -> list[tuple[str, str]]:
        """(user_id, content) for a round, oldest first."""
        async with self._db.execute(
            """
            SELECT user_id, content FROM round_posts
            WHERE game_id = ? AND round_number = ? ORDER BY id
            """,
            (game_id, round_number),
        ) as cursor:
            return [(row[0], row[1]) for row in await cursor.fetchall()]

    async def mark_acted(self, game_id: int, round_number: int, user_id: str) -> None:
        await self._db.execute(
            "INSERT OR IGNORE INTO round_actions (game_id, round_number, user_id) VALUES (?, ?, ?)",
            (game_id, round_number, user_id),
        )
        await self._db.commit()

    async def acted(self, game_id: int, round_number: int) -> set[str]:
        async with self._db.execute(
            "SELECT user_id FROM round_actions WHERE game_id = ? AND round_number = ?",
            (game_id, round_number),
        ) as cursor:
            return {row[0] for row in await cursor.fetchall()}

    # --- story bible and narration -----------------------------------------------

    async def upsert_canon(self, game_id: int, entries: list[CanonEntry]) -> None:
        await self._db.executemany(
            """
            INSERT INTO canon (game_id, kind, name, detail) VALUES (?, ?, ?, ?)
            ON CONFLICT (game_id, kind, name) DO UPDATE SET detail = excluded.detail
            """,
            [(game_id, e.kind, e.name.strip(), e.detail.strip()) for e in entries],
        )
        await self._db.commit()

    async def canon(self, game_id: int) -> list[CanonEntry]:
        async with self._db.execute(
            "SELECT kind, name, detail FROM canon WHERE game_id = ? ORDER BY kind, name",
            (game_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [CanonEntry(kind=row[0], name=row[1], detail=row[2]) for row in rows]

    async def add_narration(self, game_id: int, round_number: int, content: str) -> None:
        await self._db.execute(
            "INSERT INTO narrations (game_id, round_number, content) VALUES (?, ?, ?)",
            (game_id, round_number, content),
        )
        await self._db.commit()

    async def last_narration(self, game_id: int) -> str:
        async with self._db.execute(
            "SELECT content FROM narrations WHERE game_id = ? ORDER BY id DESC LIMIT 1",
            (game_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else ""

    async def claim_event(self, event_id: str) -> bool:
        """Claim a Discord event once across all instances sharing this DB."""
        cursor = await self._db.execute(
            "INSERT OR IGNORE INTO processed_events (event_id) VALUES (?)", (event_id,)
        )
        await self._db.commit()
        return cursor.rowcount == 1

    async def acquire_lock(self, lock_name: str, owner_id: str, ttl: float, now: float) -> bool:
        """Acquire or renew a short database-backed lease."""
        cursor = await self._db.execute(
            """
            INSERT INTO coordination_locks (lock_name, owner_id, expires_at)
            VALUES (?, ?, ?)
            ON CONFLICT(lock_name) DO UPDATE SET
                owner_id = excluded.owner_id,
                expires_at = excluded.expires_at
            WHERE coordination_locks.expires_at <= ? OR coordination_locks.owner_id = ?
            """,
            (lock_name, owner_id, now + ttl, now, owner_id),
        )
        await self._db.commit()
        return cursor.rowcount == 1

    async def release_lock(self, lock_name: str, owner_id: str) -> None:
        await self._db.execute(
            "DELETE FROM coordination_locks WHERE lock_name = ? AND owner_id = ?",
            (lock_name, owner_id),
        )
        await self._db.commit()
