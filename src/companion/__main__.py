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
from companion.llm import LLMClient
from companion.ratelimit import RateLimiter
from companion.store import ContextStore

log = logging.getLogger("companion")


async def run(settings: Settings) -> None:
    store = await ContextStore.open(settings.database_path)
    try:
        chat = ChatService(
            llm=LLMClient(settings),
            store=store,
            limiter=RateLimiter(settings.rate_limit_per_minute),
            system_prompt=settings.system_prompt,
            context_messages=settings.context_messages,
        )
        bot = CompanionBot(chat, guild_id=settings.discord_guild_id)

        # `docker stop` sends SIGTERM; close the gateway connection cleanly.
        loop = asyncio.get_running_loop()
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.ensure_future(bot.close()))

        async with bot:
            await bot.start(settings.discord_token.get_secret_value())
    finally:
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
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
