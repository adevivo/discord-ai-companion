import pytest

from companion.db import connect
from companion.game.schemas import CanonEntry, Character
from companion.game.store import GameStore, Status


@pytest.fixture
async def store():
    db = await connect(":memory:")
    yield GameStore(db)
    await db.close()


async def new_game(store, thread="t1", status=Status.LOBBY, genre="noir"):
    return await store.create_game("g1", thread, "host", status, genre)


async def test_create_and_lookup_by_thread(store):
    game = await new_game(store)
    assert game.status is Status.LOBBY
    assert game.genre == "noir"
    assert game.round_number == 0 and game.round_deadline is None and not game.nudged
    assert await store.game_for_thread("t1") == game
    assert await store.game_for_thread("nope") is None


async def test_one_game_per_thread(store):
    await new_game(store)
    with pytest.raises(Exception, match="UNIQUE"):
        await new_game(store)


async def test_update_game_including_json_fields(store):
    game = await new_game(store)
    await store.update_game(
        game.id,
        status=Status.ACTIVE,
        title="Neon Rain",
        outline={"premise": "p", "acts": ["a", "b", "c"]},
        pitches=[{"title": "x", "hook": "y"}],
        round_number=3,
        round_deadline=123.5,
        nudged=True,
    )
    game = await store.get_game(game.id)
    assert game.status is Status.ACTIVE
    assert game.title == "Neon Rain"
    assert game.outline["acts"] == ["a", "b", "c"]
    assert game.pitches == [{"title": "x", "hook": "y"}]
    assert (game.round_number, game.round_deadline, game.nudged) == (3, 123.5, True)


async def test_update_rejects_unknown_fields(store):
    game = await new_game(store)
    with pytest.raises(ValueError):
        await store.update_game(game.id, thread_id="hijack")


async def test_active_games(store):
    a = await new_game(store, "t1", Status.ACTIVE)
    await new_game(store, "t2", Status.LOBBY)
    c = await new_game(store, "t3", Status.ACTIVE)
    assert [g.id for g in await store.active_games()] == [a.id, c.id]


async def test_events_are_claimed_once(store):
    assert await store.claim_event("message:123") is True
    assert await store.claim_event("message:123") is False


async def test_coordination_lock_is_exclusive_and_expires(store):
    assert await store.acquire_lock("game:t1", "bot-a", 30, now=100) is True
    assert await store.acquire_lock("game:t1", "bot-b", 30, now=110) is False
    assert await store.acquire_lock("game:t1", "bot-b", 30, now=131) is True
    await store.release_lock("game:t1", "bot-b")
    assert await store.acquire_lock("game:t1", "bot-a", 30, now=131) is True


async def test_players_join_rejoin_and_characters(store):
    game = await new_game(store)
    await store.add_player(game.id, "u1", "Sam", "ex-cop", 0)
    await store.add_player(game.id, "u2", "Ann", "", 0)
    await store.add_player(game.id, "u1", "Sammy", "ex-cop with debts", 0)  # rejoin updates

    char = Character(name="Vex", description="A tired detective.", traits=["stubborn"])
    await store.set_character(game.id, "u1", char)

    players = await store.players(game.id)
    assert [p.user_id for p in players] == ["u1", "u2"]
    assert players[0].display_name == "Sammy"
    assert players[0].concept == "ex-cop with debts"
    assert players[0].character == char
    assert players[0].label == "Sammy (Vex)"
    assert players[1].character is None and players[1].label == "Ann"


async def test_ai_player_flag_is_persisted(store):
    game = await new_game(store)
    await store.add_player(game.id, "bot1", "Story Bot", "", 0, is_ai=True)
    player = (await store.players(game.id))[0]
    assert player.is_ai is True


async def test_posts_and_actions_are_per_round(store):
    game = await new_game(store)
    await store.add_post(game.id, 1, "u1", "I check the door.")
    await store.add_post(game.id, 1, "u2", "I keep watch.")
    await store.add_post(game.id, 2, "u1", "Later post.")
    assert await store.posts(game.id, 1) == [("u1", "I check the door."), ("u2", "I keep watch.")]

    await store.mark_acted(game.id, 1, "u1")
    await store.mark_acted(game.id, 1, "u1")  # idempotent
    assert await store.acted(game.id, 1) == {"u1"}
    assert await store.acted(game.id, 2) == set()


async def test_canon_upserts_by_kind_and_name(store):
    game = await new_game(store)
    await store.upsert_canon(
        game.id,
        [
            CanonEntry(kind="npc", name="Mara", detail="Ferry-keeper."),
            CanonEntry(kind="place", name="The Docks", detail="Foggy."),
        ],
    )
    await store.upsert_canon(
        game.id, [CanonEntry(kind="npc", name="Mara", detail="Ferry-keeper; owes smugglers.")]
    )
    canon = await store.canon(game.id)
    assert [(c.kind, c.name, c.detail) for c in canon] == [
        ("npc", "Mara", "Ferry-keeper; owes smugglers."),
        ("place", "The Docks", "Foggy."),
    ]


async def test_last_narration(store):
    game = await new_game(store)
    assert await store.last_narration(game.id) == ""
    await store.add_narration(game.id, 0, "Opening.")
    await store.add_narration(game.id, 1, "Round one.")
    assert await store.last_narration(game.id) == "Round one."


async def test_context_and_game_tables_share_a_database(tmp_path):
    from companion.store import ContextStore

    path = tmp_path / "data" / "companion.db"
    db = await connect(path)
    context, games = ContextStore(db), GameStore(db)
    await context.add_exchange("chan", "q", "a", retain=10)
    await new_game(games)
    await db.close()

    db = await connect(path)  # schema re-applies cleanly on restart
    assert len(await ContextStore(db).recent("chan", 10)) == 2
    assert await GameStore(db).game_for_thread("t1") is not None
    await db.close()
