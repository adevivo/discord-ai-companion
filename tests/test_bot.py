"""Discord adapter tests using lightweight fakes (no gateway connection)."""

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from companion.bot import (
    GENERIC_ERROR_REPLY,
    RESET_REPLY,
    CompanionBot,
    format_ask_reply,
    handle_ask,
    handle_mention,
    handle_reset,
    prepare_prompt,
)

BOT_ID = 999
NO_MENTIONS = discord.AllowedMentions.none().to_dict()


def blocks_all_mentions(allowed: discord.AllowedMentions) -> bool:
    return allowed.to_dict() == NO_MENTIONS


def http_error() -> discord.HTTPException:
    return discord.HTTPException(MagicMock(status=500, reason="err"), "boom")


class FakeChat:
    def __init__(self, reply="pong", error=None):
        self.reply = reply
        self.error = error
        self.calls = []
        self.kwargs = []
        self.reset_scopes = []

    async def respond(self, scope_id, user_id, author_name, text, **kwargs):
        self.calls.append((scope_id, user_id, author_name, text))
        self.kwargs.append(kwargs)
        if self.error:
            raise self.error
        return self.reply

    async def reset(self, scope_id):
        self.reset_scopes.append(scope_id)
        return 4


def user(user_id, name="Sam", bot=False):
    return SimpleNamespace(id=user_id, display_name=name, bot=bot)


def make_message(
    content,
    mentions,
    author=None,
    guild=True,
    type=discord.MessageType.default,
    webhook_id=None,
    typing_error=None,
):
    @contextlib.asynccontextmanager
    async def typing():
        if typing_error:
            raise typing_error
        yield

    channel = SimpleNamespace(id=42, typing=typing, send=AsyncMock())
    return SimpleNamespace(
        content=content,
        mentions=mentions,
        author=author or user(1),
        guild=SimpleNamespace(id=7) if guild else None,
        type=type,
        webhook_id=webhook_id,
        channel=channel,
        to_reference=MagicMock(return_value="REF"),
    )


def make_interaction():
    return SimpleNamespace(
        channel_id=42,
        user=user(1),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


# --- prepare_prompt / format_ask_reply -------------------------------------


@pytest.mark.parametrize("mention", [f"<@{BOT_ID}>", f"<@!{BOT_ID}>"])
def test_prepare_prompt_strips_bot_mention(mention):
    assert prepare_prompt(f"{mention}  what's up?", BOT_ID, {}) == "what's up?"


def test_prepare_prompt_names_other_users():
    text = f"<@{BOT_ID}> say hi to <@5> and <@6>"
    assert prepare_prompt(text, BOT_ID, {5: "Ann"}) == "say hi to @Ann and <@6>"


def test_prepare_prompt_preserves_newlines_and_indentation():
    text = f"<@{BOT_ID}> fix this:\n```py\ndef f():\n    return 1\n```"
    assert prepare_prompt(text, BOT_ID, {}) == "fix this:\n```py\ndef f():\n    return 1\n```"


def test_format_ask_reply_quotes_question():
    assert format_ask_reply("what is\nlife?", "42") == "> what is life?\n\n42"


def test_format_ask_reply_truncates_long_question():
    quoted = format_ask_reply("x" * 500, "a").split("\n")[0]
    assert len(quoted) <= 202
    assert quoted.endswith("…")


# --- handle_mention ----------------------------------------------------------


async def test_mention_is_answered_as_reply():
    chat = FakeChat("pong")
    msg = make_message(f"<@{BOT_ID}> ping", [user(BOT_ID, "Bot", bot=True)])

    await handle_mention(chat, msg, user(BOT_ID))

    assert chat.calls == [("42", "1", "Sam", "ping")]
    msg.to_reference.assert_called_once_with(fail_if_not_exists=False)
    msg.channel.send.assert_awaited_once()
    call = msg.channel.send.call_args
    assert call.args == ("pong",)
    assert call.kwargs["reference"] == "REF"
    assert call.kwargs["mention_author"] is False
    assert blocks_all_mentions(call.kwargs["allowed_mentions"])


async def test_reply_to_bot_message_is_answered():
    msg = make_message("and then?", [user(BOT_ID)], type=discord.MessageType.reply)
    chat = FakeChat()
    await handle_mention(chat, msg, user(BOT_ID))
    assert len(chat.calls) == 1


async def test_long_reply_is_split_and_only_first_chunk_is_a_reply():
    reply = " ".join(f"w{i}" for i in range(1500))
    chat = FakeChat(reply)
    msg = make_message(f"<@{BOT_ID}> essay", [user(BOT_ID)])

    await handle_mention(chat, msg, user(BOT_ID))

    calls = msg.channel.send.call_args_list
    assert len(calls) > 1
    assert calls[0].kwargs["reference"] == "REF"
    assert all(c.kwargs["reference"] is None for c in calls[1:])
    assert all(len(c.args[0]) <= 2000 for c in calls)
    assert all(blocks_all_mentions(c.kwargs["allowed_mentions"]) for c in calls)
    assert " ".join(c.args[0] for c in calls) == reply


async def test_reply_logs_message_count(caplog):
    caplog.set_level("INFO")
    msg = make_message(f"<@{BOT_ID}> essay", [user(BOT_ID)])
    await handle_mention(FakeChat(" ".join(f"w{i}" for i in range(1500))), msg, user(BOT_ID))
    count = msg.channel.send.await_count
    assert f"Replied (scope=42, messages={count})" in caplog.text


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mentions": []},  # not addressed to the bot
        {"mentions": [user(BOT_ID)], "author": user(2, bot=True)},  # from a bot
        {"mentions": [user(BOT_ID)], "guild": False},  # DM
        {"mentions": [user(BOT_ID)], "webhook_id": 123},  # webhook
        # "X added the bot to the thread" system message lists the bot in mentions
        {"mentions": [user(BOT_ID)], "type": discord.MessageType.recipient_add},
        {"mentions": [user(BOT_ID)], "type": discord.MessageType.thread_created},
    ],
)
async def test_ignored_messages(kwargs):
    chat = FakeChat()
    msg = make_message("hello", **kwargs)
    await handle_mention(chat, msg, user(BOT_ID))
    assert chat.calls == []
    msg.channel.send.assert_not_awaited()


async def test_unexpected_error_gets_generic_reply():
    chat = FakeChat(error=RuntimeError("boom"))
    msg = make_message(f"<@{BOT_ID}> hi", [user(BOT_ID)])
    await handle_mention(chat, msg, user(BOT_ID))
    assert msg.channel.send.call_args.args == (GENERIC_ERROR_REPLY,)


async def test_silent_reply_sends_nothing():
    msg = make_message(f"<@{BOT_ID}> hi", [user(BOT_ID)])
    await handle_mention(FakeChat(reply=None), msg, user(BOT_ID))
    msg.channel.send.assert_not_awaited()


async def test_typing_failure_does_not_block_reply():
    msg = make_message(f"<@{BOT_ID}> hi", [user(BOT_ID)], typing_error=http_error())
    await handle_mention(FakeChat("pong"), msg, user(BOT_ID))
    assert msg.channel.send.call_args.args == ("pong",)


async def test_send_failure_is_logged_not_raised(caplog):
    msg = make_message(f"<@{BOT_ID}> hi", [user(BOT_ID)])
    msg.channel.send.side_effect = http_error()
    await handle_mention(FakeChat("pong"), msg, user(BOT_ID))
    assert "Failed to send reply" in caplog.text


# --- slash commands ------------------------------------------------------------


async def test_ask_defers_then_follows_up():
    chat = FakeChat("the answer")
    interaction = make_interaction()

    await handle_ask(chat, interaction, "the question")

    interaction.response.defer.assert_awaited_once_with(thinking=True)
    assert chat.calls == [("42", "1", "Sam", "the question")]
    assert chat.kwargs == [{"suppress_repeat_notices": False}]
    call = interaction.followup.send.call_args
    assert call.args == ("> the question\n\nthe answer",)
    assert blocks_all_mentions(call.kwargs["allowed_mentions"])


async def test_ask_always_answers_the_deferred_interaction():
    interaction = make_interaction()
    await handle_ask(FakeChat(reply=None), interaction, "q")
    assert GENERIC_ERROR_REPLY in interaction.followup.send.call_args.args[0]


async def test_ask_send_failure_is_logged_not_raised(caplog):
    interaction = make_interaction()
    interaction.followup.send.side_effect = http_error()
    await handle_ask(FakeChat(), interaction, "q")
    assert "Failed to send /ask reply" in caplog.text


async def test_reset_clears_channel_and_confirms():
    chat = FakeChat()
    interaction = make_interaction()
    await handle_reset(chat, interaction)
    assert chat.reset_scopes == ["42"]
    call = interaction.response.send_message.call_args
    assert call.args == (RESET_REPLY,)
    assert blocks_all_mentions(call.kwargs["allowed_mentions"])


# --- client wiring ---------------------------------------------------------------


def test_client_is_configured_safely():
    client = CompanionBot(FakeChat(), guild_id=None)
    assert client.intents.message_content is False
    assert blocks_all_mentions(client.allowed_mentions)

    ask = client.tree.get_command("ask")
    assert ask is not None and ask.guild_only is True

    assert client.tree.get_command("help").guild_only is True
    reset = client.tree.get_command("reset")
    assert reset is not None and reset.guild_only is True
    assert reset.default_permissions.manage_messages is True


async def test_setup_hook_syncs_commands_to_guild_when_configured(monkeypatch, caplog):
    client = CompanionBot(FakeChat(), guild_id=123)
    sync = AsyncMock(return_value=["ask", "reset"])
    monkeypatch.setattr(client.tree, "sync", sync)
    caplog.set_level("INFO")

    await client.setup_hook()

    assert "Synced 2 slash commands to guild 123" in caplog.text
    guild = sync.call_args.kwargs["guild"]
    assert guild.id == 123
    names = {c.name for c in client.tree.get_commands(guild=discord.Object(id=123))}
    assert names == {"ask", "reset", "help"}  # commands were actually copied to the guild


async def test_setup_hook_syncs_globally_by_default(monkeypatch):
    client = CompanionBot(FakeChat())
    sync = AsyncMock()
    monkeypatch.setattr(client.tree, "sync", sync)
    await client.setup_hook()
    sync.assert_awaited_once_with()


async def test_setup_hook_survives_sync_failure(monkeypatch, caplog):
    client = CompanionBot(FakeChat(), guild_id=123)
    monkeypatch.setattr(client.tree, "sync", AsyncMock(side_effect=http_error()))
    await client.setup_hook()  # must not raise
    assert "Slash command sync failed" in caplog.text


# --- /help ---------------------------------------------------------------------------


def test_help_lists_every_registered_command():
    from companion.bot import build_help
    from companion.game.engine import GameService

    client = CompanionBot(FakeChat(), games=GameService(llm=None, store=None))
    text = build_help(client.tree)
    for name in ("/ask", "/reset", "/help", "/act"):
        assert f"`{name}`" in text
    for sub in ("new", "pick", "join", "begin", "status", "recap", "canon", "end"):
        assert f"`/game {sub}`" in text
    assert "How an adventure works" in text
    assert len(text) <= 2000  # fits in one Discord message


def test_help_without_game_mode_omits_game_guide():
    from companion.bot import build_help

    text = build_help(CompanionBot(FakeChat()).tree)
    assert "`/ask`" in text and "/game" not in text


async def test_help_is_private_and_uses_no_llm():
    from companion.bot import handle_help

    chat = FakeChat()
    client = CompanionBot(chat)
    interaction = make_interaction()
    await handle_help(client.tree, interaction)
    call = interaction.response.send_message.call_args
    assert call.kwargs["ephemeral"] is True
    assert blocks_all_mentions(call.kwargs["allowed_mentions"])
    assert chat.calls == []
