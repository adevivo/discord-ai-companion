import pytest

from companion.llm import ChatMessage
from companion.store import ContextStore


@pytest.fixture
async def store():
    s = await ContextStore.open(":memory:")
    yield s
    await s.close()


async def test_empty_scope_returns_nothing(store):
    assert await store.recent("chan", 10) == []


async def test_exchange_round_trip_in_order(store):
    await store.add_exchange("chan", "Sam: hi", "hello!", retain=10)
    await store.add_exchange("chan", "Sam: how are you?", "great", retain=10)
    assert await store.recent("chan", 10) == [
        ChatMessage("user", "Sam: hi"),
        ChatMessage("assistant", "hello!"),
        ChatMessage("user", "Sam: how are you?"),
        ChatMessage("assistant", "great"),
    ]


async def test_recent_returns_newest_messages(store):
    for i in range(5):
        await store.add_exchange("chan", f"q{i}", f"a{i}", retain=100)
    assert [m.content for m in await store.recent("chan", 3)] == ["a3", "q4", "a4"]


async def test_scopes_are_isolated(store):
    await store.add_exchange("general", "q", "a", retain=10)
    await store.add_exchange("thread-1", "x", "y", retain=10)
    assert [m.content for m in await store.recent("general", 10)] == ["q", "a"]
    assert [m.content for m in await store.recent("thread-1", 10)] == ["x", "y"]


async def test_prunes_to_retain_per_scope(store):
    await store.add_exchange("other", "keep", "me", retain=10)
    for i in range(10):
        await store.add_exchange("chan", f"q{i}", f"a{i}", retain=4)
    assert [m.content for m in await store.recent("chan", 100)] == ["q8", "a8", "q9", "a9"]
    assert len(await store.recent("other", 100)) == 2


async def test_zero_limit_and_retain(store):
    await store.add_exchange("chan", "q", "a", retain=0)
    assert await store.recent("chan", 10) == []
    assert await store.recent("chan", 0) == []


async def test_persists_across_reopen(tmp_path):
    path = tmp_path / "nested" / "companion.db"
    s = await ContextStore.open(path)
    await s.add_exchange("chan", "q", "a", retain=10)
    await s.close()

    s = await ContextStore.open(path)
    assert [m.content for m in await s.recent("chan", 10)] == ["q", "a"]
    await s.close()


async def test_clear_removes_only_that_scope(store):
    await store.add_exchange("chan", "q", "a", retain=10)
    await store.add_exchange("other", "q", "a", retain=10)
    assert await store.clear("chan") == 2
    assert await store.recent("chan", 10) == []
    assert len(await store.recent("other", 10)) == 2
    assert await store.clear("chan") == 0
