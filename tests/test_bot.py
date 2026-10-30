"""Discord adapter tests using lightweight fakes (no gateway connection)."""

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from companion.bot import (
    GENERIC_ERROR_REPLY,
    CompanionBot,
    format_ask_reply,
    handle_ask,
    handle_mention,
    prepare_prompt,
)

BOT_ID = 999
NO_MENTIONS = discord.AllowedMentions.none().to_dict()


def blocks_all_mentions(allowed: discord.AllowedMentions) -> bool:
    return allowed.to_dict() == NO_MENTIONS


class FakeChat:
    def __init__(self, reply="pong", error=None):
        self.reply = reply
        self.error = error
        self.calls = []

    async def respond(self, scope_id, user_id, author_name, text):
        self.calls.append((scope_id, user_id, author_name, text))
        if self.error:
            raise self.error
        return self.reply


def user(user_id, name="Sam", bot=False):
    return SimpleNamespace(id=user_id, display_name=name, bot=bot)


def make_message(content, mentions, author=None, guild=True):
    @contextlib.asynccontextmanager
    async def typing():
        yield

    channel = SimpleNamespace(id=42, typing=typing, send=AsyncMock())
    return SimpleNamespace(
        content=content,
        mentions=mentions,
        author=author or user(1),
        guild=SimpleNamespace(id=7) if guild else None,
        channel=channel,
        reply=AsyncMock(),
    )


# --- prepare_prompt / format_ask_reply -------------------------------------


@pytest.mark.parametrize("mention", [f"<@{BOT_ID}>", f"<@!{BOT_ID}>"])
def test_prepare_prompt_strips_bot_mention(mention):
    assert prepare_prompt(f"{mention}  what's up?", BOT_ID, {}) == "what's up?"


def test_prepare_prompt_names_other_users():
    text = f"<@{BOT_ID}> say hi to <@5> and <@6>"
    assert prepare_prompt(text, BOT_ID, {5: "Ann"}) == "say hi to @Ann and <@6>"


def test_format_ask_reply_quotes_question():
    assert format_ask_reply("what is\nlife?", "42") == "> what is life?\n\n42"


def test_format_ask_reply_truncates_long_question():
    quoted = format_ask_reply("x" * 500, "a").split("\n")[0]
    assert len(quoted) <= 202
    assert quoted.endswith("…")


# --- handle_mention ----------------------------------------------------------


async def test_mention_is_answered_with_reply():
    chat = FakeChat("pong")
    msg = make_message(f"<@{BOT_ID}> ping", [user(BOT_ID, "Bot", bot=True)])

    await handle_mention(chat, msg, user(BOT_ID))

    assert chat.calls == [("42", "1", "Sam", "ping")]
    msg.reply.assert_awaited_once()
    args, kwargs = msg.reply.call_args
    assert args == ("pong",)
    assert kwargs["mention_author"] is False
    assert blocks_all_mentions(kwargs["allowed_mentions"])


async def test_long_reply_is_split_across_messages():
    chat = FakeChat("word " * 1000)
    msg = make_message(f"<@{BOT_ID}> essay", [user(BOT_ID)])

    await handle_mention(chat, msg, user(BOT_ID))

    msg.reply.assert_awaited_once()
    assert msg.channel.send.await_count >= 1
    for call in msg.channel.send.call_args_list:
        assert len(call.args[0]) <= 2000
        assert blocks_all_mentions(call.kwargs["allowed_mentions"])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mentions": []},  # not addressed to the bot
        {"mentions": [user(BOT_ID)], "author": user(2, bot=True)},  # from a bot
        {"mentions": [user(BOT_ID)], "guild": False},  # DM
    ],
)
async def test_ignored_messages(kwargs):
    chat = FakeChat()
    msg = make_message("hello", **kwargs)
    await handle_mention(chat, msg, user(BOT_ID))
    assert chat.calls == []
    msg.reply.assert_not_awaited()


async def test_unexpected_error_gets_generic_reply():
    chat = FakeChat(error=RuntimeError("boom"))
    msg = make_message(f"<@{BOT_ID}> hi", [user(BOT_ID)])
    await handle_mention(chat, msg, user(BOT_ID))
    assert msg.reply.call_args.args == (GENERIC_ERROR_REPLY,)


# --- handle_ask ----------------------------------------------------------------


async def test_ask_defers_then_follows_up():
    chat = FakeChat("the answer")
    interaction = SimpleNamespace(
        channel_id=42,
        user=user(1),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    await handle_ask(chat, interaction, "the question")

    interaction.response.defer.assert_awaited_once_with(thinking=True)
    assert chat.calls == [("42", "1", "Sam", "the question")]
    call = interaction.followup.send.call_args
    assert call.args == ("> the question\n\nthe answer",)
    assert blocks_all_mentions(call.kwargs["allowed_mentions"])


# --- client wiring ---------------------------------------------------------------


def test_client_is_configured_safely():
    client = CompanionBot(FakeChat(), guild_id=None)
    assert client.intents.message_content is False
    assert blocks_all_mentions(client.allowed_mentions)
    ask = client.tree.get_command("ask")
    assert ask is not None
    assert ask.guild_only is True


async def test_setup_hook_syncs_to_guild_when_configured(monkeypatch):
    client = CompanionBot(FakeChat(), guild_id=123)
    sync = AsyncMock()
    monkeypatch.setattr(client.tree, "sync", sync)
    await client.setup_hook()
    assert sync.call_args.kwargs["guild"].id == 123


async def test_setup_hook_syncs_globally_by_default(monkeypatch):
    client = CompanionBot(FakeChat())
    sync = AsyncMock()
    monkeypatch.setattr(client.tree, "sync", sync)
    await client.setup_hook()
    sync.assert_awaited_once_with()
