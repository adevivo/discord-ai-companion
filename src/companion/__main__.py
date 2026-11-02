"""Entry point: `python -m companion`."""

import asyncio
import contextlib
import logging
import signal
import sys
import uuid

import discord
from pydantic import ValidationError

from companion.bot import CompanionBot
from companion.chat import ChatService
from companion.config import BotProfile, Settings
from companion.db import connect
from companion.game.engine import GameService
from companion.game.store import GameStore
from companion.llm import LLMClient
from companion.ratelimit import RateLimiter
from companion.store import ContextStore

log = logging.getLogger("companion")


async def run(
    settings: Settings,
    profile: BotProfile | None = None,
    games_enabled: bool = True,
    game_events_enabled: bool = True,
) -> None:
    profile = profile or settings.bot_profiles()[0]
    profile_settings = settings.model_copy(
        update={
            "discord_token": profile.discord_token,
            "discord_guild_id": profile.discord_guild_id,
            "llm_base_url": profile.llm_base_url,
            "llm_api_key": profile.llm_api_key,
            "llm_model": profile.llm_model,
        }
    )
    db = await connect(settings.database_path)
    store = ContextStore(db)
    llm = LLMClient(profile_settings)
    try:
        chat = ChatService(
            llm=llm,
            store=store,
            user_limiter=RateLimiter(profile_settings.rate_limit_per_minute),
            global_limiter=RateLimiter(profile_settings.global_rate_limit_per_minute),
            system_prompt=profile_settings.system_prompt,
            context_messages=profile_settings.context_messages,
            context_max_chars=profile_settings.context_max_chars,
        )
        games = (
            GameService(
                llm,
                GameStore(db),
                round_hours=profile_settings.game_round_hours,
                target_rounds=profile_settings.game_target_rounds,
                owner_id=f"bot-{profile.number}-{uuid.uuid4().hex}",
            )
            if games_enabled
            else None
        )
        bot = CompanionBot(
            chat,
            guild_id=profile_settings.discord_guild_id,
            games=games,
            game_events_enabled=game_events_enabled,
            ai_persona=profile.persona.prompt(),
        )

        # `docker stop` sends SIGTERM; close the gateway connection cleanly.
        # Keep a reference to the task so it can't be garbage-collected mid-flight.
        shutdown_tasks: set[asyncio.Task] = set()

        def request_shutdown() -> None:
            log.info("SIGTERM received; shutting down")
            shutdown_tasks.add(asyncio.create_task(bot.close()))

        loop = asyncio.get_running_loop()
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signal.SIGTERM, request_shutdown)

        async with bot:
            await bot.start(profile.discord_token.get_secret_value())
    finally:
        await llm.aclose()
        await store.close()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        settings = Settings()
        profiles = settings.bot_profiles()
        count = choose_profile_count(len(profiles)) if len(profiles) > 1 else 1
    except ValidationError as exc:
        missing = ", ".join(
            str(err["loc"][0]).upper() if err["loc"] else str(err["msg"])
            for err in exc.errors()
        )
        log.error("Invalid or missing configuration: %s", missing)
        return 1
    except ValueError as exc:
        log.error("Invalid Bot profile configuration: %s", exc)
        return 1

    log.info("Starting %d Bot profile(s)", count)
    try:
        if count == 1:
            asyncio.run(run(settings))
        else:
            asyncio.run(run_profiles(settings, profiles[:count]))
    except discord.LoginFailure:
        log.error("Discord rejected the bot token. Check DISCORD_TOKEN.")
        return 1
    except discord.PrivilegedIntentsRequired:
        log.error(
            "Discord refused the Message Content intent. Enable it in the Developer Portal: "
            "your app -> Bot -> Privileged Gateway Intents -> Message Content Intent."
        )
        return 1
    except KeyboardInterrupt:
        pass
    return 0


def choose_profile_count(max_count: int) -> int:
    while True:
        try:
            count = int(input(f"How many Bot profiles should run (1-{max_count})? "))
        except ValueError:
            print("Please enter a whole number.")
            continue
        if 1 <= count <= max_count:
            return count
        print(f"Please enter a number from 1 to {max_count}.")


async def run_profiles(settings: Settings, profiles: list[BotProfile]) -> None:
    await asyncio.gather(
        *(
            run(
                settings,
                profile,
                games_enabled=True,
                game_events_enabled=True,
            )
            for index, profile in enumerate(profiles)
        )
    )


if __name__ == "__main__":
    sys.exit(main())
