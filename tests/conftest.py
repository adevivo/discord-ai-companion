import pytest

from companion.config import Settings

_ENV_VARS = (
    "DISCORD_TOKEN",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "LLM_TIMEOUT_SECONDS",
    "LLM_MAX_RETRIES",
    "SYSTEM_PROMPT",
    "CONTEXT_MESSAGES",
    "RATE_LIMIT_PER_MINUTE",
    "DATABASE_PATH",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Keep tests independent of the developer's shell environment."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        discord_token="discord-secret",
        llm_api_key="llm-secret",
        llm_model="test/model",
        llm_base_url="https://llm.test/v1",
        llm_max_retries=0,
    )
