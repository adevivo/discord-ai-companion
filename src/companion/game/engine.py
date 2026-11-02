"""Game Master engine: game lifecycle, rounds, and deadlines. Independent of Discord.

Every public method returns a Result: a private message for whoever ran the
command, plus public posts for the game thread. All state changes for one game
happen under that game's lock, so a final /act and the deadline checker can
never resolve the same round twice.
"""

import asyncio
import logging
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from companion.game import prompts
from companion.game.dice import Roll, roll_2d6
from companion.game.schemas import Character, Pitches, PlayerCharacter, RoundResult, Setup
from companion.game.store import Game, GameStore, Player, Status
from companion.llm import LLMClient, LLMError
from companion.structured import complete_json

log = logging.getLogger(__name__)

OOC_PREFIXES = ("((", "//")
RETRY_SECONDS = 600
OVERTIME_ROUNDS = 2  # hard stop this many rounds past the target

NOT_A_GAME = "There's no game in this thread. Start one with `/game new` in a regular channel."
GAME_OVER = "This game has ended."
GM_UNAVAILABLE = "The GM couldn't do that right now (AI provider problem). Try again in a minute."
ALREADY_ACTED = "You already used `/act` this round."
ACT_TOO_LONG = f"Keep the `/act` action to about {prompts.MAX_ACT_WORDS} words."


@dataclass
class Result:
    message: str  # private reply to whoever ran the command
    posts: list[str] = field(default_factory=list)  # public posts to the game thread
    pings: list[str] = field(default_factory=list)  # user IDs that posts may ping
    rename: str | None = None  # new thread name


def character_card(player: Player, character: Character) -> str:
    traits = ", ".join(character.traits)
    card = f"**{character.name}** (played by {player.display_name})\n{character.description}"
    return card + (f"\n*Traits:* {traits}" if traits else "")


def lobby_post(genre: str) -> str:
    return (
        f"A new adventure is forming: *{genre or 'the GM will surprise you'}*.\n"
        "Join with `/game join` (optionally describe a character concept). "
        "When everyone's in, the host starts it with `/game begin`.\n"
        "-# Everything posted in this thread during the game is sent to the AI provider. "
        "Start a message with `((` or `//` to keep it out of the story."
    )


def round_open_post(round_number: int, deadline: float) -> str:
    return (
        f"**Round {round_number}** is open. Post what your character does and says here, "
        f"then use `/act` when you're done. Deadline: <t:{int(deadline)}:R>."
    )


class GameService:
    def __init__(
        self,
        llm: LLMClient,
        store: GameStore,
        *,
        round_hours: float = 24.0,
        target_rounds: int = 12,
        clock: Callable[[], float] = time.time,
        roll: Callable[[], Roll] = roll_2d6,
        owner_id: str | None = None,
    ) -> None:
        self._llm = llm
        self._store = store
        self._round_seconds = round_hours * 3600
        self._target_rounds = target_rounds
        self._clock = clock
        self._roll = roll
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._owner_id = owner_id or uuid.uuid4().hex

    @asynccontextmanager
    async def _game_lock(self, thread_id: str):
        lock_name = f"game:{thread_id}"
        async with self._locks[thread_id]:
            acquired = await self._store.acquire_lock(
                lock_name, self._owner_id, ttl=120.0, now=self._clock()
            )
            if not acquired:
                yield False
                return
            try:
                yield True
            finally:
                await self._store.release_lock(lock_name, self._owner_id)

    # --- setup ---------------------------------------------------------------------

    async def new_game(self, guild_id: str, thread_id: str, host_id: str, genre: str) -> Result:
        genre = genre.strip()
        if genre:
            await self._store.create_game(guild_id, thread_id, host_id, Status.LOBBY, genre)
            return Result("Game created.", posts=[lobby_post(genre)])

        try:
            pitches = await complete_json(self._llm, prompts.pitch_messages(), Pitches)
        except LLMError as exc:
            log.warning("Pitch generation failed (thread=%s): %s", thread_id, exc)
            await self._store.create_game(guild_id, thread_id, host_id, Status.LOBBY)
            return Result("Game created.", posts=[lobby_post("")])

        game = await self._store.create_game(guild_id, thread_id, host_id, Status.PITCHING)
        await self._store.update_game(game.id, pitches=[p.model_dump() for p in pitches.pitches])
        lines = [f"**{i}. {p.title}**: {p.hook}" for i, p in enumerate(pitches.pitches, 1)]
        post = (
            "The GM has three ideas:\n\n"
            + "\n".join(lines)
            + "\n\nHost: choose one with `/game pick`."
        )
        return Result("Game created.", posts=[post])

    async def pick(self, thread_id: str, user_id: str, number: int) -> Result:
        game = await self._store.game_for_thread(thread_id)
        if game is None:
            return Result(NOT_A_GAME)
        if user_id != game.host_id:
            return Result("Only the host can pick the premise.")
        if game.status is not Status.PITCHING:
            return Result("There's nothing to pick right now.")
        if not 1 <= number <= len(game.pitches):
            return Result(f"Pick a number from 1 to {len(game.pitches)}.")

        pitch = game.pitches[number - 1]
        genre = f"{pitch['title']}: {pitch['hook']}"
        await self._store.update_game(game.id, status=Status.LOBBY, genre=genre)
        return Result(
            "Premise chosen.", posts=[f"The host chose **{pitch['title']}**.\n" + lobby_post(genre)]
        )

    async def join(self, thread_id: str, user_id: str, display_name: str, concept: str) -> Result:
        concept = concept.strip()
        game = await self._store.game_for_thread(thread_id)
        if game is None:
            return Result(NOT_A_GAME)
        if game.status is Status.ENDED:
            return Result(GAME_OVER)

        players = await self._store.players(game.id)
        existing = next((p for p in players if p.user_id == user_id), None)

        if game.status in (Status.PITCHING, Status.LOBBY):
            await self._store.add_player(game.id, user_id, display_name, concept, 0)
            if existing:
                return Result("Updated your character concept.")
            return Result("You're in!", posts=[f"**{display_name}** joined the adventure."])

        if existing:
            return Result("You're already playing.")
        async with self._locks[thread_id]:
            game = await self._store.get_game(game.id)
            canon = await self._store.canon(game.id)
            messages = prompts.character_messages(game, display_name, concept, players, canon)
            try:
                character = await complete_json(self._llm, messages, Character)
            except LLMError as exc:
                log.warning("Late-join character failed (thread=%s): %s", thread_id, exc)
                return Result(GM_UNAVAILABLE)
            await self._store.add_player(game.id, user_id, display_name, concept, game.round_number)
            await self._store.set_character(game.id, user_id, character)
            player = Player(user_id, display_name, concept, character, game.round_number)
            post = "A new player joins the story!\n" + character_card(player, character)
            return Result("You're in! The GM will write you into the story.", posts=[post])

    async def begin(self, thread_id: str, user_id: str) -> Result:
        game = await self._store.game_for_thread(thread_id)
        if game is None:
            return Result(NOT_A_GAME)
        if user_id != game.host_id:
            return Result("Only the host can begin the game.")
        if game.status is Status.PITCHING:
            return Result("Pick a premise first with `/game pick`.")
        if game.status is not Status.LOBBY:
            return Result("This game has already started.")

        async with self._locks[thread_id]:
            game = await self._store.get_game(game.id)
            if game.status is not Status.LOBBY:
                return Result("This game has already started.")
            players = await self._store.players(game.id)
            if not players:
                return Result("Nobody has joined yet. Players join with `/game join`.")

            messages = prompts.setup_messages(game.genre, players, self._target_rounds)
            try:
                setup = await complete_json(self._llm, messages, Setup)
            except LLMError as exc:
                log.warning("Game setup failed (thread=%s): %s", thread_id, exc)
                return Result(GM_UNAVAILABLE)

            cards = []
            for player, character in _assign_characters(players, setup.characters):
                await self._store.set_character(game.id, player.user_id, character)
                cards.append(character_card(player, character))
            await self._store.upsert_canon(game.id, setup.canon)
            await self._store.add_narration(game.id, 0, setup.opening)

            deadline = self._clock() + self._round_seconds
            await self._store.update_game(
                game.id,
                status=Status.ACTIVE,
                title=setup.title,
                outline=setup.outline.model_dump(),
                act=1,
                round_number=1,
                round_deadline=deadline,
                nudged=False,
            )
            posts = [
                f"# {setup.title}",
                "**The cast**\n\n" + "\n\n".join(cards),
                setup.opening,
                round_open_post(1, deadline),
            ]
            return Result("The adventure begins!", posts=posts, rename=setup.title)

    # --- rounds ------------------------------------------------------------------------

    async def observe_message(self, thread_id: str, user_id: str, content: str) -> bool:
        """Record in-character chat. Returns True if the thread belongs to a game."""
        game = await self._store.game_for_thread(thread_id)
        if game is None:
            return False
        text = content.strip()
        if game.status is Status.ACTIVE and text and not text.startswith(OOC_PREFIXES):
            players = await self._store.players(game.id)
            if any(p.user_id == user_id for p in players):
                await self._store.add_post(game.id, game.round_number, user_id, text)
        return True

    async def is_ai_player(self, thread_id: str, user_id: str) -> bool:
        game = await self._store.game_for_thread(thread_id)
        if game is None:
            return False
        player = await self._store.player_for_game(game.id, user_id)
        return bool(player and player.is_ai)

    async def generate_ai_action(self, thread_id: str, user_id: str, persona: str) -> str | None:
        game = await self._store.game_for_thread(thread_id)
        if game is None or game.status is not Status.ACTIVE:
            return None
        player = await self._store.player_for_game(game.id, user_id)
        if player is None or not player.is_ai:
            return None
        if user_id in await self._store.acted(game.id, game.round_number):
            return None
        messages = prompts.ai_action_messages(
            game,
            player,
            persona,
            await self._store.canon(game.id),
            await self._store.last_narration(game.id),
        )
        try:
            action = (await self._llm.complete(messages)).strip()
        except LLMError as exc:
            log.warning(
                "AI player action failed (thread=%s, player=%s): %s", thread_id, user_id, exc
            )
            return None
        words = action.split()
        return " ".join(words[: prompts.MAX_ACT_WORDS]) or None

    async def claim_event(self, event_id: str) -> bool:
        return await self._store.claim_event(event_id)

    async def invite_ai(
        self, thread_id: str, user_id: str, display_name: str, concept: str = ""
    ) -> Result:
        """Add a mentioned Discord bot only after a human explicitly invites it."""
        game = await self._store.game_for_thread(thread_id)
        if game is None:
            return Result(NOT_A_GAME)
        if game.status is Status.ENDED:
            return Result(GAME_OVER)

        players = await self._store.players(game.id)
        existing = next((p for p in players if p.user_id == user_id), None)
        if existing is not None:
            return Result(f"{display_name} is already in this adventure.")

        if game.status in (Status.PITCHING, Status.LOBBY):
            await self._store.add_player(
                game.id, user_id, display_name, concept.strip(), 0, is_ai=True
            )
            return Result(
                f"{display_name} was invited as an AI player.",
                posts=[f"**{display_name}** was invited to the adventure by a human player."],
            )

        if game.status is not Status.ACTIVE:
            return Result("There's no round in progress.")

        async with self._game_lock(thread_id) as acquired:
            if not acquired:
                return Result("Another Bot instance is processing this game. Try again shortly.")
            game = await self._store.get_game(game.id)
            if game is None or game.status is not Status.ACTIVE:
                return Result("There's no round in progress.")
            players = await self._store.players(game.id)
            if any(p.user_id == user_id for p in players):
                return Result(f"{display_name} is already in this adventure.")
            canon = await self._store.canon(game.id)
            messages = prompts.character_messages(
                game, display_name, concept.strip(), players, canon
            )
            try:
                character = await complete_json(self._llm, messages, Character)
            except LLMError as exc:
                log.warning("AI player character failed (thread=%s): %s", thread_id, exc)
                return Result(GM_UNAVAILABLE)
            await self._store.add_player(
                game.id, user_id, display_name, concept.strip(), game.round_number, is_ai=True
            )
            await self._store.set_character(game.id, user_id, character)
            player = Player(
                user_id, display_name, concept.strip(), character, game.round_number, is_ai=True
            )
            return Result(
                f"{display_name} joined as an AI player.",
                posts=[
                    "A new AI player joins the story!\n" + character_card(player, character),
                    round_open_post(game.round_number, game.round_deadline or self._clock()),
                ],
            )

    async def act(self, thread_id: str, user_id: str, note: str = "") -> Result:
        game = await self._store.game_for_thread(thread_id)
        if game is None:
            return Result(NOT_A_GAME)
        if game.status is not Status.ACTIVE:
            return Result("There's no round in progress.")

        async with self._game_lock(thread_id) as acquired:
            if not acquired:
                return Result("Another Bot instance is processing this game. Try again shortly.")
            game = await self._store.get_game(game.id)
            players = await self._store.players(game.id)
            player = next((p for p in players if p.user_id == user_id), None)
            if player is None:
                return Result("You're not in this game. Join with `/game join`.")

            note = note.strip()
            if len(note.split()) > prompts.MAX_ACT_WORDS:
                return Result(ACT_TOO_LONG)
            acted = await self._store.acted(game.id, game.round_number)
            if user_id in acted:
                return Result(ALREADY_ACTED)
            if note:
                await self._store.add_post(game.id, game.round_number, user_id, note)
            await self._store.mark_acted(game.id, game.round_number, user_id)
            acted.add(user_id)
            ready = f"✅ **{player.label}** is ready ({len(acted)}/{len(players)})."

            if any(p.user_id not in acted for p in players):
                return Result("Got it. The round resolves when everyone's ready.", posts=[ready])
            resolution = await self._resolve(game, players)
            return Result(
                "Everyone's ready; the GM has resolved the round.", posts=[ready, *resolution]
            )

    async def tick(self) -> list[tuple[str, Result]]:
        """Resolve rounds past their deadline and send halfway reminders."""
        out = []
        for game in await self._store.active_games():
            try:
                result = await self._tick_game(game)
            except Exception:
                log.exception("Deadline check failed (thread=%s)", game.thread_id)
                continue
            if result and result.posts:
                out.append((game.thread_id, result))
        return out

    async def _tick_game(self, game: Game) -> Result | None:
        now = self._clock()
        if game.round_deadline is None:
            return None

        if now >= game.round_deadline:
            async with self._game_lock(game.thread_id) as acquired:
                if not acquired:
                    return None
                game = await self._store.get_game(game.id)
                if game.status is not Status.ACTIVE or now < (game.round_deadline or now + 1):
                    return None  # resolved by a final /act while we waited
                players = await self._store.players(game.id)
                return Result("", posts=await self._resolve(game, players))

        if not game.nudged and now >= game.round_deadline - self._round_seconds / 2:
            await self._store.update_game(game.id, nudged=True)
            players = await self._store.players(game.id)
            acted = await self._store.acted(game.id, game.round_number)
            waiting = [p for p in players if p.user_id not in acted]
            if waiting:
                names = ", ".join(f"<@{p.user_id}>" for p in waiting)
                post = (
                    f"⏰ Round {game.round_number} closes <t:{int(game.round_deadline)}:R>. "
                    f"Still waiting on: {names}"
                )
                return Result("", posts=[post], pings=[p.user_id for p in waiting])
        return None

    async def _resolve(self, game: Game, players: list[Player]) -> list[str]:
        """Narrate the current round and open the next. Caller holds the game's lock."""
        rolls = {p.user_id: self._roll() for p in players}
        messages = prompts.round_messages(
            game,
            players,
            await self._store.posts(game.id, game.round_number),
            rolls,
            await self._store.canon(game.id),
            await self._store.last_narration(game.id),
            self._target_rounds,
        )
        try:
            result = await complete_json(self._llm, messages, RoundResult)
        except LLMError as exc:
            failures = game.resolve_failures + 1
            log.error(
                "Round resolution failed (thread=%s, attempt %d): %s", game.thread_id, failures, exc
            )
            await self._store.update_game(
                game.id,
                round_deadline=self._clock() + RETRY_SECONDS,
                resolve_failures=failures,
            )
            if failures == 1:
                return [
                    "⏳ The GM is having trouble reaching the AI provider and will retry shortly."
                ]
            return []

        await self._store.upsert_canon(game.id, result.canon_updates)
        await self._store.add_narration(game.id, game.round_number, result.narration)

        posts = []
        used = _players_named(players, result.rolls_used)
        if used:
            posts.append(
                "🎲 " + " · ".join(f"{p.label}: {rolls[p.user_id].describe()}" for p in used)
            )
        posts.append(result.narration)

        if result.game_over or game.round_number >= self._target_rounds + OVERTIME_ROUNDS:
            await self._store.update_game(
                game.id,
                status=Status.ENDED,
                round_deadline=None,
                summary=result.summary,
                act=result.act,
                resolve_failures=0,
            )
            posts.append("🏁 **The End.** Thanks for playing!")
            return posts

        next_round = game.round_number + 1
        deadline = self._clock() + self._round_seconds
        await self._store.update_game(
            game.id,
            round_number=next_round,
            round_deadline=deadline,
            nudged=False,
            resolve_failures=0,
            summary=result.summary,
            act=result.act,
        )
        posts.append(round_open_post(next_round, deadline))
        return posts

    # --- information and control -----------------------------------------------------------

    async def status(self, thread_id: str) -> Result:
        game = await self._store.game_for_thread(thread_id)
        if game is None:
            return Result(NOT_A_GAME)
        players = await self._store.players(game.id)
        names = ", ".join(p.label for p in players) or "nobody yet"
        if game.status is not Status.ACTIVE:
            return Result(f"Status: **{game.status.value}**. Players: {names}.")
        acted = await self._store.acted(game.id, game.round_number)
        waiting = ", ".join(p.label for p in players if p.user_id not in acted) or "nobody"
        return Result(
            f"**{game.title}**, round {game.round_number} of about {self._target_rounds} "
            f"(act {game.act}). Deadline <t:{int(game.round_deadline or 0)}:R>.\n"
            f"Players: {names}\nStill to `/act`: {waiting}"
        )

    async def recap(self, thread_id: str) -> Result:
        game = await self._store.game_for_thread(thread_id)
        if game is None:
            return Result(NOT_A_GAME)
        if game.status in (Status.PITCHING, Status.LOBBY):
            return Result("The story hasn't started yet.")
        summary = game.summary or "The adventure has just begun."
        return Result(f"**{game.title}: the story so far**\n\n{summary}")

    async def canon(self, thread_id: str) -> Result:
        game = await self._store.game_for_thread(thread_id)
        if game is None:
            return Result(NOT_A_GAME)
        entries = await self._store.canon(game.id)
        if not entries:
            return Result("Nothing has been established yet.")
        lines = [f"- **{c.name}** ({c.kind}): {c.detail}" for c in entries]
        return Result("**Story bible**\n" + "\n".join(lines))

    async def end(self, thread_id: str, user_id: str, is_moderator: bool = False) -> Result:
        game = await self._store.game_for_thread(thread_id)
        if game is None:
            return Result(NOT_A_GAME)
        if user_id != game.host_id and not is_moderator:
            return Result("Only the host (or a moderator) can end the game.")
        if game.status is Status.ENDED:
            return Result(GAME_OVER)
        async with self._game_lock(thread_id) as acquired:
            if not acquired:
                return Result("Another Bot instance is processing this game. Try again shortly.")
            await self._store.update_game(game.id, status=Status.ENDED, round_deadline=None)
        return Result("Game ended.", posts=["🏁 The host has ended this adventure."])

    async def abandon(self, thread_id: str) -> None:
        """End a game whose thread no longer exists, so it stops consuming LLM calls."""
        game = await self._store.game_for_thread(thread_id)
        if game is not None and game.status is not Status.ENDED:
            log.warning("Abandoning game whose thread is gone (thread=%s)", thread_id)
            async with self._game_lock(thread_id) as acquired:
                if not acquired:
                    return
                await self._store.update_game(game.id, status=Status.ENDED, round_deadline=None)


def _assign_characters(
    players: list[Player], characters: list[PlayerCharacter]
) -> list[tuple[Player, Character]]:
    """Match generated characters to players by name, falling back to order."""
    remaining = list(characters)
    assigned: dict[str, Character] = {}
    for player in players:
        match = next(
            (c for c in remaining if c.player.strip().casefold() == player.display_name.casefold()),
            None,
        )
        if match:
            remaining.remove(match)
            assigned[player.user_id] = match
    out = []
    for player in players:
        character = assigned.get(player.user_id)
        if character is None:
            character = (
                remaining.pop(0)
                if remaining
                else Character(name=player.display_name, description="A newcomer to the story.")
            )
        out.append(
            (player, Character(**character.model_dump(include={"name", "description", "traits"})))
        )
    return out


def _players_named(players: list[Player], names: list[str]) -> list[Player]:
    """Players referenced by display name, character name, or label (case-insensitive)."""
    wanted = {n.strip().casefold() for n in names}
    out = []
    for p in players:
        keys = {p.display_name.casefold(), p.label.casefold()}
        if p.character:
            keys.add(p.character.name.casefold())
        if keys & wanted:
            out.append(p)
    return out
