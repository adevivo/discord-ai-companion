"""Game Master Discord adapter tests (fakes only, no gateway)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from companion.bot import CompanionBot
from companion.game.commands import (
    GENERIC_ERROR,
    NEEDS_TEXT_CHANNEL,
    THREAD_FAILED,
    deliver_due,
    handle_game_message,
    handle_new_game,
    run_command,
    send_result,
)
from companion.game.engine import Result

NO_MENTIONS = discord.AllowedMentions.none().to_dict()


def http_error(cls=discord.HTTPException):
    return cls(MagicMock(status=404, reason="err"), "boom")


class FakeGames:
    def __init__(self):
        self.observed = []
        self.abandoned = []
        self.tick_results = []
        self.new_game = AsyncMock(return_value=Result("Game created.", posts=["Welcome!"]))

    async def observe_message(self, thread_id, user_id, content):
        self.observed.append((thread_id, user_id, content))
        return thread_id == "500"

    async def tick(self):
        return self.tick_results

    async def abandon(self, thread_id):
        self.abandoned.append(thread_id)


def make_interaction(channel=None):
    return SimpleNamespace(
        channel=channel
        if channel is not None
        else SimpleNamespace(send=AsyncMock(), edit=AsyncMock()),
        channel_id=500,
        guild_id=7,
        user=SimpleNamespace(id=1, display_name="Sam"),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def make_message(channel_parent=123, **overrides):
    values = dict(
        author=SimpleNamespace(id=1, bot=False),
        webhook_id=None,
        guild=SimpleNamespace(id=7),
        type=discord.MessageType.default,
        channel=SimpleNamespace(id=500, parent_id=channel_parent),
        content="Vex kicks the door.",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


# --- send_result -------------------------------------------------------------------


async def test_send_result_splits_and_blocks_mentions():
    channel = SimpleNamespace(send=AsyncMock())
    await send_result(channel, Result("", posts=["short", "x " * 1500]))
    sent = [c.args[0] for c in channel.send.call_args_list]
    assert sent[0] == "short" and len(sent) >= 3
    assert all(len(s) <= 2000 for s in sent)
    for call in channel.send.call_args_list:
        assert call.kwargs["allowed_mentions"].to_dict() == NO_MENTIONS


async def test_send_result_pings_only_listed_users():
    channel = SimpleNamespace(send=AsyncMock())
    await send_result(channel, Result("", posts=["<@42> wake up"], pings=["42"]))
    allowed = channel.send.call_args.kwargs["allowed_mentions"].to_dict()
    assert allowed["users"] == [42]
    assert allowed["parse"] == []  # no @everyone, no roles


# --- run_command -------------------------------------------------------------------


async def test_run_command_replies_privately_and_posts_publicly():
    interaction = make_interaction()
    action = AsyncMock(return_value=Result("Got it.", posts=["✅ Sam is ready."]))

    await run_command(interaction, action)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    private = interaction.followup.send.call_args
    assert private.args == ("Got it.",) and private.kwargs["ephemeral"] is True
    interaction.channel.send.assert_awaited_once()
    assert interaction.channel.send.call_args.args == ("✅ Sam is ready.",)


async def test_run_command_renames_thread():
    interaction = make_interaction()
    await run_command(interaction, AsyncMock(return_value=Result("ok", rename="Neon Rain")))
    interaction.channel.edit.assert_awaited_once_with(name="🏮 Neon Rain")


async def test_run_command_engine_error_is_generic():
    interaction = make_interaction()
    await run_command(interaction, AsyncMock(side_effect=RuntimeError("boom")))
    assert interaction.followup.send.call_args.args == (GENERIC_ERROR,)
    interaction.channel.send.assert_not_awaited()


# --- /game new ----------------------------------------------------------------------


async def test_new_game_creates_thread_and_posts_there():
    thread = SimpleNamespace(id=900, mention="<#900>", send=AsyncMock())
    channel = MagicMock(spec=discord.TextChannel)
    channel.create_thread = AsyncMock(return_value=thread)
    interaction = make_interaction(channel)
    games = FakeGames()

    await handle_new_game(games, interaction, "cyberpunk")

    kwargs = channel.create_thread.call_args.kwargs
    assert kwargs["type"] is discord.ChannelType.public_thread
    assert kwargs["auto_archive_duration"] == 10080
    games.new_game.assert_awaited_once_with("7", "900", "1", "cyberpunk")
    assert "<#900>" in interaction.followup.send.call_args.args[0]
    assert thread.send.call_args.args == ("Welcome!",)


async def test_new_game_rejects_non_text_channels():
    interaction = make_interaction(SimpleNamespace(parent_id=1))  # e.g. inside a thread
    await handle_new_game(FakeGames(), interaction, "")
    interaction.response.send_message.assert_awaited_once_with(NEEDS_TEXT_CHANNEL, ephemeral=True)


async def test_new_game_reports_missing_thread_permission():
    channel = MagicMock(spec=discord.TextChannel)
    channel.create_thread = AsyncMock(side_effect=http_error(discord.Forbidden))
    interaction = make_interaction(channel)
    games = FakeGames()
    await handle_new_game(games, interaction, "")
    assert interaction.followup.send.call_args.args == (THREAD_FAILED,)
    games.new_game.assert_not_awaited()


# --- thread chat -----------------------------------------------------------------------


async def test_thread_chat_goes_to_engine():
    games = FakeGames()
    assert await handle_game_message(games, make_message()) is True
    assert games.observed == [("500", "1", "Vex kicks the door.")]


@pytest.mark.parametrize(
    "overrides",
    [
        {"channel": SimpleNamespace(id=500, parent_id=None)},  # regular channel
        {"author": SimpleNamespace(id=2, bot=True)},
        {"webhook_id": 99},
        {"guild": None},
        {"type": discord.MessageType.thread_created},
    ],
)
async def test_non_game_messages_are_not_observed(overrides):
    games = FakeGames()
    assert await handle_game_message(games, make_message(**overrides)) is False
    assert games.observed == []


async def test_bot_routes_game_threads_away_from_chat(monkeypatch):
    games = FakeGames()
    bot = CompanionBot(SimpleNamespace(), games=games)
    monkeypatch.setattr(CompanionBot, "user", SimpleNamespace(id=999))
    handled = AsyncMock()
    monkeypatch.setattr("companion.bot.handle_mention", handled)

    await bot.on_message(make_message())  # thread 500 is a game thread
    handled.assert_not_awaited()

    other = make_message(channel=SimpleNamespace(id=501, parent_id=123))
    await bot.on_message(other)  # a thread without a game
    handled.assert_awaited_once()


# --- deadlines ---------------------------------------------------------------------------


async def test_deliver_due_posts_to_threads():
    games = FakeGames()
    channel = SimpleNamespace(send=AsyncMock())
    games.tick_results = [("500", Result("", posts=["Narration."]))]
    await deliver_due(games, AsyncMock(return_value=channel))
    assert channel.send.call_args.args == ("Narration.",)


async def test_deliver_due_abandons_deleted_threads_and_continues():
    games = FakeGames()
    alive = SimpleNamespace(send=AsyncMock())
    games.tick_results = [("500", Result("", posts=["a"])), ("501", Result("", posts=["b"]))]

    async def get_channel(channel_id):
        if channel_id == 500:
            raise http_error(discord.NotFound)
        return alive

    await deliver_due(games, get_channel)
    assert games.abandoned == ["500"]
    assert alive.send.call_args.args == ("b",)


# --- client wiring -----------------------------------------------------------------------


def test_game_mode_enables_message_content_and_registers_commands():
    bot = CompanionBot(SimpleNamespace(), games=FakeGames())
    assert bot.intents.message_content is True

    game = bot.tree.get_command("game")
    assert game is not None and game.guild_only is True
    assert {c.name for c in game.commands} == {
        "new",
        "pick",
        "join",
        "begin",
        "status",
        "recap",
        "canon",
        "end",
    }
    act = bot.tree.get_command("act")
    assert act is not None and act.guild_only is True
    assert bot.tree.get_command("ask") is not None


def test_chat_only_bot_keeps_message_content_off():
    bot = CompanionBot(SimpleNamespace())
    assert bot.intents.message_content is False
    assert bot.tree.get_command("game") is None
