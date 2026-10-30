"""Discord adapter: turns @mentions and /ask into ChatService calls.

Kept deliberately thin. The handler functions take duck-typed Discord objects
so they can be tested without a gateway connection.

Security notes:
- Only bot-directed messages are processed; the privileged Message Content
  intent is not requested.
- All outgoing messages use AllowedMentions.none(), so LLM output can never
  ping @everyone, roles, or users.
- Server-only: DMs are ignored and /ask is guild-only.
"""

import logging
import re

import discord
from discord import app_commands

from companion.chat import ChatService
from companion.splitter import split_message

log = logging.getLogger(__name__)

NO_MENTIONS = discord.AllowedMentions.none()
GENERIC_ERROR_REPLY = "Sorry, something went wrong on my end."
_USER_MENTION = re.compile(r"<@!?(\d+)>")
_ASK_QUOTE_MAX = 200


def prepare_prompt(content: str, bot_id: int, mention_names: dict[int, str]) -> str:
    """Remove the bot's own mention and replace other user mentions with @names."""

    def replace(match: re.Match[str]) -> str:
        user_id = int(match.group(1))
        if user_id == bot_id:
            return ""
        return f"@{mention_names[user_id]}" if user_id in mention_names else match.group(0)

    return " ".join(_USER_MENTION.sub(replace, content).split())


def format_ask_reply(prompt: str, reply: str) -> str:
    """Quote the question above the answer, since /ask options aren't visible in chat."""
    quote = " ".join(prompt.split())
    if len(quote) > _ASK_QUOTE_MAX:
        quote = quote[: _ASK_QUOTE_MAX - 1] + "…"
    return f"> {quote}\n\n{reply}"


async def safe_respond(chat: ChatService, scope_id: str, user_id: str, name: str, text: str) -> str:
    try:
        return await chat.respond(scope_id, user_id, name, text)
    except Exception:
        log.exception("Unhandled error while responding (scope=%s)", scope_id)
        return GENERIC_ERROR_REPLY


async def handle_mention(chat: ChatService, message, bot_user) -> None:
    if message.author.bot or message.guild is None:
        return
    if not any(user.id == bot_user.id for user in message.mentions):
        return

    names = {user.id: user.display_name for user in message.mentions}
    prompt = prepare_prompt(message.content, bot_user.id, names)

    async with message.channel.typing():
        reply = await safe_respond(
            chat,
            str(message.channel.id),
            str(message.author.id),
            message.author.display_name,
            prompt,
        )

    chunks = split_message(reply)
    await message.reply(chunks[0], mention_author=False, allowed_mentions=NO_MENTIONS)
    for chunk in chunks[1:]:
        await message.channel.send(chunk, allowed_mentions=NO_MENTIONS)


async def handle_ask(chat: ChatService, interaction, prompt: str) -> None:
    await interaction.response.defer(thinking=True)
    reply = await safe_respond(
        chat,
        str(interaction.channel_id),
        str(interaction.user.id),
        interaction.user.display_name,
        prompt,
    )
    for chunk in split_message(format_ask_reply(prompt, reply)):
        await interaction.followup.send(chunk, allowed_mentions=NO_MENTIONS)


class CompanionBot(discord.Client):
    def __init__(self, chat: ChatService, guild_id: int | None = None) -> None:
        intents = discord.Intents.default()
        intents.message_content = False  # bot-directed messages only
        super().__init__(intents=intents, allowed_mentions=NO_MENTIONS)
        self.chat = chat
        self.guild_id = guild_id
        self.tree = app_commands.CommandTree(self)
        self._register_commands()

    def _register_commands(self) -> None:
        @self.tree.command(name="ask", description="Ask the AI a question")
        @app_commands.describe(prompt="What you want to ask")
        @app_commands.guild_only()
        async def ask(interaction: discord.Interaction, prompt: str) -> None:
            await handle_ask(self.chat, interaction, prompt)

    async def setup_hook(self) -> None:
        if self.guild_id is not None:
            # Guild sync is instant; handy during development.
            guild = discord.Object(id=self.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self) -> None:
        log.info("Connected as %s (guilds=%d)", self.user, len(self.guilds))

    async def on_message(self, message: discord.Message) -> None:
        if self.user is not None:
            await handle_mention(self.chat, message, self.user)
