"""JSON shapes the GM's LLM calls must return.

Validation is deliberately lenient where the model is likely to wobble
(unknown canon kinds become "fact", extra fields are ignored) and strict where
the game would break (missing narration, wrong number of pitches).
"""

from pydantic import BaseModel, Field, field_validator

CANON_KINDS = ("npc", "place", "item", "fact", "thread")


class CanonEntry(BaseModel):
    kind: str = "fact"
    name: str = Field(min_length=1)
    detail: str = Field(min_length=1)

    @field_validator("kind", mode="before")
    @classmethod
    def _normalize_kind(cls, value: object) -> str:
        kind = str(value or "").strip().lower()
        return kind if kind in CANON_KINDS else "fact"


class Pitch(BaseModel):
    title: str = Field(min_length=1)
    hook: str = Field(min_length=1)


class Pitches(BaseModel):
    pitches: list[Pitch] = Field(min_length=3, max_length=3)


class Character(BaseModel):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    traits: list[str] = Field(default_factory=list)


class PlayerCharacter(Character):
    player: str = Field(min_length=1, description="The player's display name")


class OutlineNpc(BaseModel):
    name: str
    secret: str = ""


class Outline(BaseModel):
    premise: str = Field(min_length=1)
    acts: list[str] = Field(min_length=3, max_length=4)
    npcs: list[OutlineNpc] = Field(default_factory=list)
    endings: list[str] = Field(default_factory=list)


class Setup(BaseModel):
    title: str = Field(min_length=1)
    outline: Outline
    characters: list[PlayerCharacter]
    opening: str = Field(min_length=1)
    canon: list[CanonEntry] = Field(default_factory=list)


class RoundResult(BaseModel):
    narration: str = Field(min_length=1)
    rolls_used: list[str] = Field(default_factory=list, description="Player display names")
    canon_updates: list[CanonEntry] = Field(default_factory=list)
    summary: str = Field(min_length=1)
    act: int = 1
    game_over: bool = False

    @field_validator("act", mode="after")
    @classmethod
    def _clamp_act(cls, value: int) -> int:
        return min(max(value, 1), 4)
