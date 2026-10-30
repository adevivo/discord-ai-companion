"""Application settings, loaded from environment variables (or a local .env file).

Secrets are typed as SecretStr so they are masked in repr/str/logs. Use
`.get_secret_value()` only at the point where the secret is actually needed.
"""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SYSTEM_PROMPT = (
    "You are a friendly, concise assistant in a Discord server. "
    "Keep replies conversational and use Discord markdown where helpful."
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Discord
    discord_token: SecretStr

    # LLM (any OpenAI-compatible endpoint)
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: SecretStr
    llm_model: str = Field(min_length=1)
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_max_retries: int = Field(default=1, ge=0)

    # Behaviour
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    context_messages: int = Field(default=20, ge=0)
    rate_limit_per_minute: int = Field(default=5, ge=1)

    # Storage
    database_path: Path = Path("data/companion.db")
