"""Discord adapter: turns @mentions, /ask and /reset into ChatService calls,
and hosts Game Master mode (see companion.game.commands).

Kept deliberately thin. The handler functions take duck-typed Discord objects
so they can be tested without a gateway connection.

Security notes:
- Chat mode only processes bot-directed messages. The Message Content intent
  is enabled for Game Master mode, but message text is only used inside
  active game threads; everywhere else it is ignored.
- All outgoing messages use AllowedMentions.none(), so LLM output can never
  ping @everyone, roles, or users.
- Server-only: DMs are ignored and slash commands are guild-only.
"""

import asyncio
import contextlib
import logging
import re

import discord
from discord import app_commands

from companion.chat import ChatService
from companion.game.commands import (
    deliver_due,
    handle_ai_request,
    handle_game_message,
    register_game_commands,
)
from companion.game.engine import GameService
from companion.splitter import split_message

log = logging.getLogger(__name__)

NO_MENTIONS = discord.AllowedMentions.none()
GENERIC_ERROR_REPLY = "Sorry, something went wrong on my end."
RESET_REPLY = "Conversation memory for this channel has been cleared."
_USER_MESSAGE_TYPES = (discord.MessageType.default, discord.MessageType.reply)
_USER_MENTION = re.compile(r"<@!?(\d+)>")
_ASK_QUOTE_MAX = 200
DEADLINE_CHECK_SECONDS = 60


def prepare_prompt(content: str, bot_id: int, mention_names: dict[int, str]) -> str:
    """Remove the bot's own mention and replace other user mentions with @names.

    Newlines and indentation are preserved (pasted code, lists, stack traces).
    """

    def replace(match: re.Match[str]) -> str:
        user_id = int(match.group(1))
        if user_id == bot_id:
            return ""
        return f"@{mention_names[user_id]}" if user_id in mention_names else match.group(0)

    return _USER_MENTION.sub(replace, content).strip()


def format_ask_reply(prompt: str, reply: str) -> str:
    """Quote the question above the answer, since /ask options aren't visible in chat."""
    quote = " ".join(prompt.split())
    if len(quote) > _ASK_QUOTE_MAX:
        quote = quote[: _ASK_QUOTE_MAX - 1] + "…"
    return f"> {quote}\n\n{reply}"


async def safe_respond(chat: ChatService, *args, **kwargs) -> str | None:
    try:
        return await chat.respond(*args, **kwargs)
    except Exception:
        log.exception("Unhandled error while responding")
        return GENERIC_ERROR_REPLY


def is_addressed_to_bot(message, bot_user) -> bool:
    return (
        not message.author.bot
        and message.webhook_id is None
        and message.guild is not None
        and message.type in _USER_MESSAGE_TYPES  # skip "X added Y to the thread" etc.
        and any(user.id == bot_user.id for user in message.mentions)
    )


async def handle_mention(chat: ChatService, message, bot_user) -> None:
    if not is_addressed_to_bot(message, bot_user):
        return

    names = {user.id: user.display_name for user in message.mentions}
    prompt = prepare_prompt(message.content, bot_user.id, names)

    async with contextlib.AsyncExitStack() as stack:
        with contextlib.suppress(discord.HTTPException):
            await stack.enter_async_context(message.channel.typing())  # cosmetic only
        reply = await safe_respond(
            chat,
            str(message.channel.id),
            str(message.author.id),
            message.author.display_name,
            prompt,
        )
    if reply is None:
        return

    # If the user deleted their message while we waited, post without the reply link.
    reference = message.to_reference(fail_if_not_exists=False)
    chunks = split_message(reply)
    try:
        for i, chunk in enumerate(chunks):
            await message.channel.send(
                chunk,
                reference=reference if i == 0 else None,
                mention_author=False,
                allowed_mentions=NO_MENTIONS,
            )
    except discord.HTTPException:
        log.exception("Failed to send reply (scope=%s)", message.channel.id)
        return
    log.info("Replied (scope=%s, messages=%d)", message.channel.id, len(chunks))


async def handle_ask(chat: ChatService, interaction, prompt: str) -> None:
    await interaction.response.defer(thinking=True)
    reply = await safe_respond(
        chat,
        str(interaction.channel_id),
        str(interaction.user.id),
        interaction.user.display_name,
        prompt,
        suppress_repeat_notices=False,  # the deferred "thinking" state needs an answer
    )
    chunks = split_message(format_ask_reply(prompt, reply or GENERIC_ERROR_REPLY))
    try:
        for chunk in chunks:
            await interaction.followup.send(chunk, allowed_mentions=NO_MENTIONS)
    except discord.HTTPException:
        log.exception("Failed to send /ask reply (scope=%s)", interaction.channel_id)
        return
    log.info("Answered /ask (scope=%s, messages=%d)", interaction.channel_id, len(chunks))


HOW_TO_PLAY = (
    "**How an adventure works**\n"
    "1. `/game new` in a regular channel opens a thread (leave genre blank for GM pitches).\n"
    "2. Everyone runs `/game join` in the thread; the host runs `/game begin`.\n"
    "3. Each round, write what your character does in the thread, then `/act`.\n"
    "4. The round resolves when everyone has acted, or at the deadline.\n"
    "-# Start a message with `((` or `//` to keep it out of the story."
)


def build_help(tree: app_commands.CommandTree) -> str:
    """Command list generated from what's actually registered, so it can't go stale."""
    lines = ["**Commands**", "`@mention` me to chat in any channel"]
    for command in tree.get_commands():
        if isinstance(command, app_commands.Group):
            lines += [
                f"`/{command.name} {sub.name}`: {sub.description}" for sub in command.commands
            ]
        else:
            lines.append(f"`/{command.name}`: {command.description}")
    if tree.get_command("game") is not None:
        lines += ["", HOW_TO_PLAY]
    return "\n".join(lines)


async def handle_help(tree: app_commands.CommandTree, interaction) -> None:
    await interaction.response.send_message(
        build_help(tree), ephemeral=True, allowed_mentions=NO_MENTIONS
    )


async def handle_reset(chat: ChatService, interaction) -> None:
    removed = await chat.reset(str(interaction.channel_id))
    log.info("Context reset (scope=%s, messages=%d)", interaction.channel_id, removed)
    await interaction.response.send_message(RESET_REPLY, allowed_mentions=NO_MENTIONS)


class CompanionBot(discord.Client):
    def __init__(
        self,
        chat: ChatService,
        guild_id: int | None = None,
        games: GameService | None = None,
        game_events_enabled: bool = True,
        ai_persona: str = "",
    ) -> None:
        intents = discord.Intents.default()
        # Needed to read in-character chat in game threads; ignored elsewhere.
        intents.message_content = games is not None
        super().__init__(intents=intents, allowed_mentions=NO_MENTIONS)
        self.chat = chat
        self.games = games
        self.game_events_enabled = game_events_enabled
        self.ai_persona = ai_persona
        self.guild_id = guild_id
        self.tree = app_commands.CommandTree(self)
        self._register_commands()
        if games is not None:
            register_game_commands(self.tree, games)
        self._deadline_task: asyncio.Task | None = None

    def _register_commands(self) -> None:
        @self.tree.command(name="ask", description="Ask the AI a question")
        @app_commands.describe(prompt="What you want to ask")
        @app_commands.guild_only()
        async def ask(interaction: discord.Interaction, prompt: str) -> None:
            await handle_ask(self.chat, interaction, prompt)

        @self.tree.command(name="reset", description="Clear the AI's memory of this channel")
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_messages=True)
        async def reset(interaction: discord.Interaction) -> None:
            await handle_reset(self.chat, interaction)

        @self.tree.command(name="help", description="What I can do and how to play")
        @app_commands.guild_only()
        async def help_(interaction: discord.Interaction) -> None:
            await handle_help(self.tree, interaction)

    async def setup_hook(self) -> None:
        if self.games is not None and self.game_events_enabled:
            self._deadline_task = asyncio.create_task(self._deadline_loop())
        try:
            if self.guild_id is not None:
                # Guild sync is instant; handy during development.
                guild = discord.Object(id=self.guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("Synced %d slash commands to guild %s", len(synced), self.guild_id)
            else:
                synced = await self.tree.sync()
                log.info(
                    "Synced %d slash commands globally (may take a while to appear)", len(synced)
                )
        except discord.HTTPException:
            # Don't crash-loop: @mentions still work without slash commands.
            log.exception(
                "Slash command sync failed; check DISCORD_GUILD_ID and that the bot was "
                "invited with the applications.commands scope"
            )

    async def on_ready(self) -> None:
        log.info("Connected as %s (guilds=%d)", self.user, len(self.guilds))

    async def on_message(self, message: discord.Message) -> None:
        if self.user is None:
            return
        if self.games is not None:
            if await handle_ai_request(self.games, message, self.user, self.ai_persona):
                return
        # Game threads belong to the GM; chat mode doesn't answer there.
        if self.games is not None and await handle_game_message(self.games, message):
            return
        await handle_mention(self.chat, message, self.user)

    async def close(self) -> None:
        if self._deadline_task is not None:
            self._deadline_task.cancel()
        await super().close()

    async def _get_channel(self, channel_id: int):
        return self.get_channel(channel_id) or await self.fetch_channel(channel_id)

    async def _deadline_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await deliver_due(self.games, self._get_channel)
            except Exception:
                log.exception("Deadline check failed")
            await asyncio.sleep(DEADLINE_CHECK_SECONDS)
