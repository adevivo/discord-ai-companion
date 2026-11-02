import asyncio

import pytest

from companion import chat as chat_mod
from companion.chat import ChatService
from companion.llm import ChatMessage, LLMError, LLMRateLimitError, LLMTimeoutError
from companion.ratelimit import RateLimiter
from companion.store import ContextStore


class FakeLLM:
    def __init__(self, reply: str = "ok", error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[list[ChatMessage]] = []

    async def complete(self, messages):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return self.reply


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def store():
    s = await ContextStore.open(":memory:")
    yield s
    await s.close()


def make_service(
    store,
    llm,
    per_user=100,
    global_=100,
    context_messages=10,
    context_max_chars=100_000,
    clock=None,
):
    clock = clock or FakeClock()
    return ChatService(
        llm=llm,
        store=store,
        user_limiter=RateLimiter(per_user, clock=clock),
        global_limiter=RateLimiter(global_, clock=clock),
        system_prompt="Be nice.",
        context_messages=context_messages,
        context_max_chars=context_max_chars,
        clock=clock,
    )


def history_sent(llm, call=0) -> list[str]:
    """Contents between the system prompt and the new user message."""
    return [m.content for m in llm.calls[call][1:-1]]


# --- prompt building and storage -------------------------------------------------


async def test_builds_prompt_and_stores_exchange(store):
    llm = FakeLLM("Hello Sam!")
    service = make_service(store, llm)

    assert await service.respond("chan", "u1", "Sam", "  hi there  ") == "Hello Sam!"

    sent = llm.calls[0]
    assert sent[0].role == "system"
    assert sent[0].content.startswith("Be nice.")
    assert "display name" in sent[0].content
    assert "`/help`" in sent[0].content and "`/game new`" in sent[0].content
    assert sent[-1] == ChatMessage("user", "Sam: hi there")
    assert await store.recent("chan", 10) == [
        ChatMessage("user", "Sam: hi there"),
        ChatMessage("assistant", "Hello Sam!"),
    ]


async def test_preserves_multiline_prompts(store):
    llm = FakeLLM()
    await make_service(store, llm).respond("chan", "u1", "Sam", "fix:\n    return 1")
    assert llm.calls[0][-1].content == "Sam: fix:\n    return 1"


async def test_includes_prior_context_from_same_scope_only(store):
    await store.add_exchange("chan", "Ann: earlier", "earlier reply", retain=10)
    await store.add_exchange("elsewhere", "Bob: secret", "x", retain=10)
    llm = FakeLLM()
    await make_service(store, llm).respond("chan", "u1", "Sam", "now")
    assert history_sent(llm) == ["Ann: earlier", "earlier reply"]


async def test_context_is_limited_by_message_count(store):
    for i in range(10):
        await store.add_exchange("chan", f"q{i}", f"a{i}", retain=100)
    llm = FakeLLM()
    await make_service(store, llm, context_messages=4).respond("chan", "u1", "Sam", "now")
    assert history_sent(llm) == ["q8", "a8", "q9", "a9"]


async def test_context_is_limited_by_char_budget(store):
    for i in range(5):
        await store.add_exchange("chan", f"q{i}" + "x" * 98, f"a{i}" + "y" * 98, retain=100)
    llm = FakeLLM()
    service = make_service(store, llm, context_max_chars=450)
    await service.respond("chan", "u1", "Sam", "now")
    # 4 messages (400 chars) fit; the oldest kept message must be a user turn.
    assert [c[:2] for c in history_sent(llm)] == ["q3", "a3", "q4", "a4"]


@pytest.mark.parametrize("context_messages", [1, 3, 5])
async def test_history_never_starts_with_assistant_turn(store, context_messages):
    for i in range(5):
        await store.add_exchange("chan", f"q{i}", f"a{i}", retain=100)
    llm = FakeLLM()
    service = make_service(store, llm, context_messages=context_messages)
    await service.respond("chan", "u1", "Sam", "now")
    sent = llm.calls[0][1:-1]
    assert not sent or sent[0].role == "user"


async def test_store_failure_still_returns_reply(store, caplog):
    llm = FakeLLM("answer")
    service = make_service(store, llm)

    async def broken(*args, **kwargs):
        raise OSError("disk full")

    store.add_exchange = broken
    assert await service.respond("chan", "u1", "Sam", "hi") == "answer"
    assert "Failed to store exchange" in caplog.text


async def test_concurrent_requests_in_same_scope_see_each_other(store):
    gate = asyncio.Event()

    class SlowLLM(FakeLLM):
        async def complete(self, messages):
            self.calls.append(messages)
            if len(self.calls) == 1:
                await gate.wait()
            return f"reply{len(self.calls)}"

    llm = SlowLLM()
    service = make_service(store, llm)
    first = asyncio.create_task(service.respond("chan", "u1", "Sam", "one"))
    second = asyncio.create_task(service.respond("chan", "u2", "Ann", "two"))
    await asyncio.sleep(0.01)
    assert len(llm.calls) == 1  # second request waits for the first
    gate.set()
    await asyncio.gather(first, second)

    assert history_sent(llm, call=1) == ["Sam: one", "reply1"]


async def test_reset_clears_scope(store):
    await store.add_exchange("chan", "q", "a", retain=10)
    await store.add_exchange("other", "q", "a", retain=10)
    service = make_service(store, FakeLLM())
    assert await service.reset("chan") == 2
    assert await store.recent("chan", 10) == []
    assert len(await store.recent("other", 10)) == 2


# --- input validation ----------------------------------------------------------------


async def test_empty_prompt_skips_llm(store):
    llm = FakeLLM()
    reply = await make_service(store, llm).respond("chan", "u1", "Sam", "   ")
    assert reply == chat_mod.EMPTY_PROMPT_REPLY
    assert llm.calls == []


async def test_overlong_prompt_skips_llm(store):
    llm = FakeLLM()
    text = "x" * (chat_mod.MAX_PROMPT_CHARS + 1)
    reply = await make_service(store, llm).respond("chan", "u1", "Sam", text)
    assert reply == chat_mod.PROMPT_TOO_LONG_REPLY
    assert llm.calls == []


# --- rate limiting -------------------------------------------------------------------


async def test_user_rate_limit_notifies_once_per_window(store):
    clock = FakeClock()
    llm = FakeLLM()
    service = make_service(store, llm, per_user=1, clock=clock)

    assert await service.respond("chan", "u1", "Sam", "one") == "ok"
    notice = await service.respond("chan", "u1", "Sam", "two")
    assert notice.startswith("You're sending requests too quickly")
    assert await service.respond("chan", "u1", "Sam", "three") is None  # silent
    assert len(llm.calls) == 1

    assert await service.respond("chan", "u2", "Ann", "hi") == "ok"  # others unaffected

    clock.now += 60
    assert await service.respond("chan", "u1", "Sam", "four") == "ok"


async def test_repeat_notices_can_be_requested(store):
    service = make_service(store, FakeLLM(), per_user=1)
    await service.respond("chan", "u1", "Sam", "one")
    for _ in range(2):
        reply = await service.respond("chan", "u1", "Sam", "x", suppress_repeat_notices=False)
        assert reply.startswith("You're sending requests too quickly")


async def test_global_rate_limit_applies_across_users(store):
    llm = FakeLLM()
    service = make_service(store, llm, global_=2)
    assert await service.respond("chan", "u1", "Sam", "a") == "ok"
    assert await service.respond("chan", "u2", "Ann", "b") == "ok"
    reply = await service.respond("chan", "u3", "Bob", "c")
    assert reply.startswith("I'm handling a lot of requests")
    assert len(llm.calls) == 2


async def test_blocked_requests_do_not_consume_quota(store):
    clock = FakeClock()
    service = make_service(store, FakeLLM(), per_user=1, global_=2, clock=clock)
    await service.respond("chan", "u1", "Sam", "a")
    await service.respond("chan", "u1", "Sam", "blocked by user limit")
    # u1's blocked attempt must not have used the second global slot
    assert await service.respond("chan", "u2", "Ann", "b") == "ok"


# --- LLM failures ------------------------------------------------------------------


@pytest.mark.parametrize(
    "error,expected",
    [
        (LLMRateLimitError("x"), chat_mod.PROVIDER_BUSY_REPLY),
        (LLMTimeoutError("x"), chat_mod.TIMEOUT_REPLY),
        (LLMError("x"), chat_mod.LLM_ERROR_REPLY),
    ],
)
async def test_llm_failures_are_friendly_and_not_stored(store, error, expected):
    service = make_service(store, FakeLLM(error=error))
    assert await service.respond("chan", "u1", "Sam", "hi") == expected
    assert await store.recent("chan", 10) == []
