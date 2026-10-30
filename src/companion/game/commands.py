"""Discord adapter for Game Master mode: slash commands, thread chat, deadlines.

Thin by design: every decision lives in GameService. Command confirmations are
private (ephemeral); everything the table should see is posted to the thread.
Mentions are disabled everywhere except halfway reminders, which may ping only
the specific players the engine lists (never anything from LLM output).
"""

import contextlib
import logging
from collections.abc import Awaitable, Callable

import discord
from discord import app_commands

from companion.game.engine import GameService, Result
from companion.splitter import split_message

log = logging.getLogger(__name__)

NO_MENTIONS = discord.AllowedMentions.none()
GENERIC_ERROR = "Sorry, something went wrong on my end."
NEEDS_TEXT_CHANNEL = "Run `/game new` in a regular text channel (not a thread or DM)."
THREAD_FAILED = (
    "I couldn't create a thread for the game. I need the **Create Public Threads** permission."
)
THREAD_NAME_PREFIX = "🏮 "
ONE_WEEK_MINUTES = 10080
_USER_MESSAGE_TYPES = (discord.MessageType.default, discord.MessageType.reply)


async def send_result(channel, result: Result) -> None:
    """Post a result's public posts to a channel, split to Discord's limits."""
    if result.pings:
        allowed = discord.AllowedMentions(
            everyone=False, roles=False, users=[discord.Object(int(u)) for u in result.pings]
        )
    else:
        allowed = NO_MENTIONS
    for post in result.posts:
        for chunk in split_message(post):
            await channel.send(chunk, allowed_mentions=allowed)


async def run_command(interaction, action: Callable[[], Awaitable[Result]], channel=None) -> None:
    """Defer privately, run the engine action, reply privately, post publicly."""
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        result = await action()
    except Exception:
        log.exception("Game command failed")
        result = Result(GENERIC_ERROR)

    target = channel if channel is not None else interaction.channel
    try:
        for chunk in split_message(result.message):
            await interaction.followup.send(chunk, ephemeral=True, allowed_mentions=NO_MENTIONS)
        if result.rename:
            with contextlib.suppress(discord.HTTPException):
                await target.edit(name=(THREAD_NAME_PREFIX + result.rename)[:100])
        await send_result(target, result)
    except discord.HTTPException:
        log.exception("Failed to deliver game command result")


async def handle_new_game(games: GameService, interaction, genre: str) -> None:
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message(NEEDS_TEXT_CHANNEL, ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        thread = await channel.create_thread(
            name=THREAD_NAME_PREFIX + "New adventure",
            type=discord.ChannelType.public_thread,
            auto_archive_duration=ONE_WEEK_MINUTES,
            reason=f"Game started by {interaction.user}",
        )
    except discord.HTTPException:
        log.exception("Could not create game thread (channel=%s)", channel.id)
        await interaction.followup.send(THREAD_FAILED, ephemeral=True)
        return

    try:
        result = await games.new_game(
            str(interaction.guild_id), str(thread.id), str(interaction.user.id), genre
        )
    except Exception:
        log.exception("Creating game failed (thread=%s)", thread.id)
        result = Result(GENERIC_ERROR)
    try:
        await interaction.followup.send(
            f"{result.message} Head over to {thread.mention}.", ephemeral=True
        )
        await send_result(thread, result)
    except discord.HTTPException:
        log.exception("Failed to deliver new game (thread=%s)", thread.id)


async def handle_game_message(games: GameService, message) -> bool:
    """Feed in-character chat to the engine. True if the message was in a game thread."""
    if (
        message.author.bot
        or message.webhook_id is not None
        or message.guild is None
        or message.type not in _USER_MESSAGE_TYPES
        or getattr(message.channel, "parent_id", None) is None  # only threads host games
    ):
        return False
    return await games.observe_message(
        str(message.channel.id), str(message.author.id), message.content
    )


async def deliver_due(games: GameService, get_channel: Callable[[int], Awaitable]) -> None:
    """Run one deadline check and post the results to their threads."""
    for thread_id, result in await games.tick():
        try:
            channel = await get_channel(int(thread_id))
            await send_result(channel, result)
        except discord.NotFound:
            await games.abandon(thread_id)
        except discord.HTTPException:
            log.exception("Failed to post deadline result (thread=%s)", thread_id)


def _is_moderator(interaction) -> bool:
    permissions = getattr(interaction.user, "guild_permissions", None)
    return bool(permissions and permissions.manage_messages)


def register_game_commands(tree: app_commands.CommandTree, games: GameService) -> None:
    game = app_commands.Group(
        name="game", description="Story adventures run by the AI Game Master", guild_only=True
    )

    def thread_id(interaction) -> str:
        return str(interaction.channel_id)

    @game.command(name="new", description="Start a new adventure in its own thread")
    @app_commands.describe(genre="Genre or premise; leave blank for the GM to pitch ideas")
    async def new(interaction: discord.Interaction, genre: str = "") -> None:
        await handle_new_game(games, interaction, genre)

    @game.command(name="pick", description="Host: choose one of the GM's pitched premises")
    @app_commands.describe(number="Which pitch (1-3)")
    async def pick(interaction: discord.Interaction, number: app_commands.Range[int, 1, 3]) -> None:
        await run_command(
            interaction,
            lambda: games.pick(thread_id(interaction), str(interaction.user.id), number),
        )

    @game.command(name="join", description="Join the adventure in this thread")
    @app_commands.describe(concept="Optional idea for your character, e.g. 'ex-cop with debts'")
    async def join(interaction: discord.Interaction, concept: str = "") -> None:
        await run_command(
            interaction,
            lambda: games.join(
                thread_id(interaction),
                str(interaction.user.id),
                interaction.user.display_name,
                concept,
            ),
        )

    @game.command(name="begin", description="Host: start the adventure")
    async def begin(interaction: discord.Interaction) -> None:
        await run_command(
            interaction, lambda: games.begin(thread_id(interaction), str(interaction.user.id))
        )

    @game.command(name="status", description="Round, deadline, and who still needs to act")
    async def status(interaction: discord.Interaction) -> None:
        await run_command(interaction, lambda: games.status(thread_id(interaction)))

    @game.command(name="recap", description="The story so far")
    async def recap(interaction: discord.Interaction) -> None:
        await run_command(interaction, lambda: games.recap(thread_id(interaction)))

    @game.command(name="canon", description="Established facts: people, places, things")
    async def canon(interaction: discord.Interaction) -> None:
        await run_command(interaction, lambda: games.canon(thread_id(interaction)))

    @game.command(name="end", description="Host or moderator: end the adventure")
    async def end(interaction: discord.Interaction) -> None:
        await run_command(
            interaction,
            lambda: games.end(
                thread_id(interaction), str(interaction.user.id), _is_moderator(interaction)
            ),
        )

    @app_commands.command(name="act", description="Mark yourself ready for this round")
    @app_commands.describe(note="Optional final words or action to add")
    @app_commands.guild_only()
    async def act(interaction: discord.Interaction, note: str = "") -> None:
        await run_command(
            interaction, lambda: games.act(thread_id(interaction), str(interaction.user.id), note)
        )

    tree.add_command(game)
    tree.add_command(act)
