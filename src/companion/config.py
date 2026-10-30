"""Application settings, loaded from environment variables (or a local .env file).

Secrets are typed as SecretStr so they are masked in repr/str/logs. Use
`.get_secret_value()` only at the point where the secret is actually needed.
"""

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SYSTEM_PROMPT = (
    "You are a friendly, concise assistant in a Discord server. "
    "Keep replies conversational and use Discord markdown where helpful."
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Discord
    discord_token: SecretStr = Field(min_length=1)
    # If set, slash commands sync to this server instantly instead of globally.
    discord_guild_id: int | None = None

    # LLM (any OpenAI-compatible endpoint)
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: SecretStr = Field(min_length=1)
    llm_model: str = Field(min_length=1)
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

    @field_validator("discord_guild_id", "llm_max_tokens", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # `DISCORD_GUILD_ID=` in .env means "not set", not "invalid integer".
        if isinstance(value, str) and not value.strip():
            return None
        return value
