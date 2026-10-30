import pytest
from pydantic import BaseModel

from companion.game.schemas import CanonEntry, Pitches, RoundResult
from companion.llm import ChatMessage, LLMError
from companion.structured import complete_json, parse_json_reply


class Thing(BaseModel):
    name: str
    count: int


class ScriptedLLM:
    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[list[ChatMessage]] = []

    async def complete(self, messages):
        self.calls.append(messages)
        return self.replies.pop(0)


PROMPT = [ChatMessage("system", "s"), ChatMessage("user", "give json")]


@pytest.mark.parametrize(
    "reply",
    [
        '{"name": "a", "count": 1}',
        'Sure! Here you go:\n{"name": "a", "count": 1}\nHope that helps.',
        '```json\n{"name": "a", "count": 1}\n```',
        '```\n{"name": "a", "count": 1}\n```',
    ],
    ids=["bare", "chatter", "json-fence", "plain-fence"],
)
def test_parse_tolerates_wrappers(reply):
    assert parse_json_reply(reply, Thing) == Thing(name="a", count=1)


@pytest.mark.parametrize("reply", ["no json here", '{"name": "a"}', '{"name": "a", "count": "x"}'])
def test_parse_rejects_bad_replies(reply):
    with pytest.raises(ValueError):
        parse_json_reply(reply, Thing)


async def test_complete_json_first_try():
    llm = ScriptedLLM('{"name": "a", "count": 1}')
    assert await complete_json(llm, PROMPT, Thing) == Thing(name="a", count=1)
    assert len(llm.calls) == 1


async def test_complete_json_retries_with_feedback():
    llm = ScriptedLLM('{"name": "a"}', '{"name": "a", "count": 2}')
    assert await complete_json(llm, PROMPT, Thing) == Thing(name="a", count=2)

    retry = llm.calls[1]
    assert retry[:2] == PROMPT
    assert retry[2] == ChatMessage("assistant", '{"name": "a"}')
    assert "count" in retry[3].content  # the validation error is fed back
    assert "ONLY the corrected JSON" in retry[3].content


async def test_complete_json_gives_up():
    llm = ScriptedLLM("nope", "still nope", "never")
    with pytest.raises(LLMError, match="3 attempts"):
        await complete_json(llm, PROMPT, Thing)


async def test_feedback_does_not_accumulate_across_retries():
    llm = ScriptedLLM("bad1", "bad2", '{"name": "a", "count": 3}')
    await complete_json(llm, PROMPT, Thing)
    assert len(llm.calls[2]) == 4  # original prompt + last bad reply + feedback only


# --- schema leniency -------------------------------------------------------------


def test_unknown_canon_kind_becomes_fact():
    assert CanonEntry(kind="Villain", name="x", detail="y").kind == "fact"
    assert CanonEntry(kind=" NPC ", name="x", detail="y").kind == "npc"


def test_round_result_act_is_clamped_and_defaults_apply():
    result = RoundResult.model_validate({"narration": "n", "summary": "s", "act": 9})
    assert result.act == 4
    assert result.rolls_used == [] and result.canon_updates == [] and not result.game_over


def test_pitches_require_exactly_three():
    with pytest.raises(ValueError):
        Pitches.model_validate({"pitches": [{"title": "a", "hook": "b"}]})
