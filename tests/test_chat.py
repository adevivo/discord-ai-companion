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


@pytest.fixture
async def store():
    s = await ContextStore.open(":memory:")
    yield s
    await s.close()


def make_service(store, llm, max_requests=100, context_messages=10):
    return ChatService(
        llm=llm,
        store=store,
        limiter=RateLimiter(max_requests),
        system_prompt="Be nice.",
        context_messages=context_messages,
    )


async def test_builds_prompt_and_stores_exchange(store):
    llm = FakeLLM("Hello Sam!")
    service = make_service(store, llm)

    assert await service.respond("chan", "u1", "Sam", "  hi there  ") == "Hello Sam!"

    sent = llm.calls[0]
    assert sent[0].role == "system"
    assert sent[0].content.startswith("Be nice.")
    assert "display name" in sent[0].content
    assert sent[-1] == ChatMessage("user", "Sam: hi there")
    assert await store.recent("chan", 10) == [
        ChatMessage("user", "Sam: hi there"),
        ChatMessage("assistant", "Hello Sam!"),
    ]


async def test_includes_prior_context_from_same_scope_only(store):
    await store.add_exchange("chan", "Ann: earlier", "earlier reply", retain=10)
    await store.add_exchange("elsewhere", "Bob: secret", "x", retain=10)
    llm = FakeLLM()
    await make_service(store, llm).respond("chan", "u1", "Sam", "now")

    contents = [m.content for m in llm.calls[0][1:]]
    assert contents == ["Ann: earlier", "earlier reply", "Sam: now"]


async def test_context_is_limited(store):
    for i in range(10):
        await store.add_exchange("chan", f"q{i}", f"a{i}", retain=100)
    llm = FakeLLM()
    await make_service(store, llm, context_messages=4).respond("chan", "u1", "Sam", "now")
    assert [m.content for m in llm.calls[0][1:-1]] == ["q8", "a8", "q9", "a9"]


async def test_empty_prompt_skips_llm(store):
    llm = FakeLLM()
    assert await make_service(store, llm).respond("chan", "u1", "Sam", "   ") == (
        chat_mod.EMPTY_PROMPT_REPLY
    )
    assert llm.calls == []


async def test_overlong_prompt_skips_llm(store):
    llm = FakeLLM()
    text = "x" * (chat_mod.MAX_PROMPT_CHARS + 1)
    reply = await make_service(store, llm).respond("chan", "u1", "Sam", text)
    assert reply == chat_mod.PROMPT_TOO_LONG_REPLY
    assert llm.calls == []


async def test_user_rate_limit(store):
    llm = FakeLLM()
    service = make_service(store, llm, max_requests=1)
    await service.respond("chan", "u1", "Sam", "one")
    reply = await service.respond("chan", "u1", "Sam", "two")
    assert reply.startswith("You're sending requests too quickly")
    assert len(llm.calls) == 1
    # a different user is unaffected
    assert await service.respond("chan", "u2", "Ann", "hi") == "ok"


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
