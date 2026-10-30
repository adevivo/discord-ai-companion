"""Entry point: `python -m companion`."""

import asyncio
import contextlib
import logging
import signal
import sys

import discord
from pydantic import ValidationError

from companion.bot import CompanionBot
from companion.chat import ChatService
from companion.config import Settings
from companion.db import connect
from companion.game.engine import GameService
from companion.game.store import GameStore
from companion.llm import LLMClient
from companion.ratelimit import RateLimiter
from companion.store import ContextStore

log = logging.getLogger("companion")


async def run(settings: Settings) -> None:
    db = await connect(settings.database_path)
    store = ContextStore(db)
    llm = LLMClient(settings)
    try:
        chat = ChatService(
            llm=llm,
            store=store,
            user_limiter=RateLimiter(settings.rate_limit_per_minute),
            global_limiter=RateLimiter(settings.global_rate_limit_per_minute),
            system_prompt=settings.system_prompt,
            context_messages=settings.context_messages,
            context_max_chars=settings.context_max_chars,
        )
        games = GameService(
            llm,
            GameStore(db),
            round_hours=settings.game_round_hours,
            target_rounds=settings.game_target_rounds,
        )
        bot = CompanionBot(chat, guild_id=settings.discord_guild_id, games=games)

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
            await bot.start(settings.discord_token.get_secret_value())
    finally:
        await llm.aclose()
        await store.close()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        settings = Settings()
    except ValidationError as exc:
        missing = ", ".join(str(err["loc"][0]).upper() for err in exc.errors())
        log.error("Invalid or missing configuration: %s", missing)
        return 1

    log.info("Starting (model=%s, base_url=%s)", settings.llm_model, settings.llm_base_url)
    try:
        asyncio.run(run(settings))
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


if __name__ == "__main__":
    sys.exit(main())
