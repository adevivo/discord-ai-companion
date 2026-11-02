"""Application settings, loaded from environment variables (or a local .env file).

Secrets are typed as SecretStr so they are masked in repr/str/logs. Use
`.get_secret_value()` only at the point where the secret is actually needed.
"""

from dataclasses import dataclass
from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from companion.game.ai_roles import AIPersona, persona_for

DEFAULT_SYSTEM_PROMPT = (
    "You are a friendly, concise assistant in a Discord server. "
    "Keep replies conversational and use Discord markdown where helpful."
)


@dataclass(frozen=True)
class BotProfile:
    number: int
    discord_token: SecretStr
    discord_guild_id: int | None
    llm_base_url: str
    llm_api_key: SecretStr
    llm_model: str
    persona: AIPersona


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Discord
    discord_token: SecretStr | None = Field(default=None, min_length=1)
    # If set, slash commands sync to this server instantly instead of globally.
    discord_guild_id: int | None = None
    discord_guild_id_1: int | None = None
    discord_guild_id_2: int | None = None
    discord_guild_id_3: int | None = None
    discord_guild_id_4: int | None = None
    discord_guild_id_5: int | None = None
    discord_guild_id_6: int | None = None
    discord_guild_id_7: int | None = None
    discord_guild_id_8: int | None = None
    discord_guild_id_9: int | None = None
    discord_guild_id_10: int | None = None

    # LLM (any OpenAI-compatible endpoint)
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: SecretStr | None = Field(default=None, min_length=1)
    llm_model: str | None = Field(default=None, min_length=1)
    # Optional additional Bot profiles. Profile 1 falls back to the legacy
    # unnumbered values above, so existing .env files keep working.
    discord_token_1: SecretStr | None = None
    discord_token_2: SecretStr | None = None
    discord_token_3: SecretStr | None = None
    discord_token_4: SecretStr | None = None
    discord_token_5: SecretStr | None = None
    discord_token_6: SecretStr | None = None
    discord_token_7: SecretStr | None = None
    discord_token_8: SecretStr | None = None
    discord_token_9: SecretStr | None = None
    discord_token_10: SecretStr | None = None
    llm_base_url_1: str | None = None
    llm_base_url_2: str | None = None
    llm_base_url_3: str | None = None
    llm_base_url_4: str | None = None
    llm_base_url_5: str | None = None
    llm_base_url_6: str | None = None
    llm_base_url_7: str | None = None
    llm_base_url_8: str | None = None
    llm_base_url_9: str | None = None
    llm_base_url_10: str | None = None
    llm_api_key_1: SecretStr | None = None
    llm_api_key_2: SecretStr | None = None
    llm_api_key_3: SecretStr | None = None
    llm_api_key_4: SecretStr | None = None
    llm_api_key_5: SecretStr | None = None
    llm_api_key_6: SecretStr | None = None
    llm_api_key_7: SecretStr | None = None
    llm_api_key_8: SecretStr | None = None
    llm_api_key_9: SecretStr | None = None
    llm_api_key_10: SecretStr | None = None
    llm_model_1: str | None = None
    llm_model_2: str | None = None
    llm_model_3: str | None = None
    llm_model_4: str | None = None
    llm_model_5: str | None = None
    llm_model_6: str | None = None
    llm_model_7: str | None = None
    llm_model_8: str | None = None
    llm_model_9: str | None = None
    llm_model_10: str | None = None
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_max_retries: int = Field(default=1, ge=0)
    # Cap on reply length in tokens; unset lets the provider decide.
    llm_max_tokens: int | None = Field(default=None, gt=0)

    # Behaviour
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    context_messages: int = Field(default=20, ge=0)
    # Upper bound on history sent to the LLM, so context can't outgrow the model.
    context_max_chars: int = Field(default=24_000, ge=0)
    rate_limit_per_minute: int = Field(default=5, ge=1)
    # Across all users: protects the provider account's quota.
    global_rate_limit_per_minute: int = Field(default=20, ge=1)

    # Game Master mode
    game_round_hours: float = Field(default=24.0, gt=0)
    game_target_rounds: int = Field(default=12, ge=3)

    # Storage
    database_path: Path = Path("data/companion.db")

    @field_validator(
        "discord_guild_id",
        "discord_guild_id_1",
        "discord_guild_id_2",
        "discord_guild_id_3",
        "discord_guild_id_4",
        "discord_guild_id_5",
        "discord_guild_id_6",
        "discord_guild_id_7",
        "discord_guild_id_8",
        "discord_guild_id_9",
        "discord_guild_id_10",
        "llm_max_tokens",
        "discord_token_1",
        "discord_token_2",
        "discord_token_3",
        "discord_token_4",
        "discord_token_5",
        "discord_token_6",
        "discord_token_7",
        "discord_token_8",
        "discord_token_9",
        "discord_token_10",
        "llm_api_key_1",
        "llm_api_key_2",
        "llm_api_key_3",
        "llm_api_key_4",
        "llm_api_key_5",
        "llm_api_key_6",
        "llm_api_key_7",
        "llm_api_key_8",
        "llm_api_key_9",
        "llm_api_key_10",
        "llm_base_url_1",
        "llm_base_url_2",
        "llm_base_url_3",
        "llm_base_url_4",
        "llm_base_url_5",
        "llm_base_url_6",
        "llm_base_url_7",
        "llm_base_url_8",
        "llm_base_url_9",
        "llm_base_url_10",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # `DISCORD_GUILD_ID=` in .env means "not set", not "invalid integer".
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="before")
    @classmethod
    def _require_legacy_or_numbered_config(cls, values: object) -> object:
        if not isinstance(values, dict):
            return values
        values = dict(values)
        numbered = values.get("discord_token_1") or values.get("DISCORD_TOKEN_1")
        if numbered:
            values.setdefault("discord_token", numbered)
            values.setdefault("llm_api_key", values.get("llm_api_key_1"))
            values.setdefault("llm_model", values.get("llm_model_1"))
        missing = [
            name
            for name in ("discord_token", "llm_api_key", "llm_model")
            if not values.get(name)
        ]
        if missing:
            names = ", ".join(f"{name.upper()} ({name})" for name in missing)
            raise ValueError("Missing required configuration: " + names)
        return values

    def bot_profiles(self) -> list[BotProfile]:
        profiles: list[BotProfile] = []
        for number in range(1, 11):
            token = getattr(self, f"discord_token_{number}")
            base_url = getattr(self, f"llm_base_url_{number}")
            api_key = getattr(self, f"llm_api_key_{number}")
            model = getattr(self, f"llm_model_{number}")
            if number == 1 and all(value is None for value in (token, base_url, api_key, model)):
                profiles.append(
                    BotProfile(
                        number=1,
                        discord_token=self.discord_token,
                        discord_guild_id=self.discord_guild_id_1
                        if self.discord_guild_id_1 is not None
                        else self.discord_guild_id,
                        llm_base_url=self.llm_base_url,
                        llm_api_key=self.llm_api_key,
                        llm_model=self.llm_model,
                        persona=persona_for(1),
                    )
                )
                continue
            if all(value is None for value in (token, base_url, api_key, model)):
                break
            if token is None or api_key is None or not model:
                raise ValueError(
                    f"Profile {number} requires DISCORD_TOKEN_{number}, "
                    f"LLM_API_KEY_{number}, and LLM_MODEL_{number}"
                )
            profiles.append(
                BotProfile(
                    number=number,
                    discord_token=token,
                    discord_guild_id=getattr(self, f"discord_guild_id_{number}")
                    if getattr(self, f"discord_guild_id_{number}") is not None
                    else self.discord_guild_id,
                    llm_base_url=base_url or self.llm_base_url,
                    llm_api_key=api_key,
                    llm_model=model,
                    persona=persona_for(number),
                )
            )
        return profiles
