from pathlib import Path

import discord
import pytest
from pydantic import ValidationError

from companion import __main__ as entry
from companion.__main__ import main
from companion.config import Settings

REQUIRED = {"DISCORD_TOKEN": "discord-secret", "LLM_API_KEY": "llm-secret", "LLM_MODEL": "m"}


def _set_env(monkeypatch, **overrides):
    for name, value in {**REQUIRED, **overrides}.items():
        monkeypatch.setenv(name, value)


def test_loads_from_environment_with_defaults(monkeypatch):
    _set_env(monkeypatch)
    s = Settings(_env_file=None)
    assert s.discord_token.get_secret_value() == "discord-secret"
    assert s.llm_api_key.get_secret_value() == "llm-secret"
    assert s.llm_base_url == "https://openrouter.ai/api/v1"
    assert s.context_messages == 20
    assert s.database_path == Path("data/companion.db")


@pytest.mark.parametrize("missing", sorted(REQUIRED))
def test_missing_required_value_fails(monkeypatch, missing):
    _set_env(monkeypatch)
    monkeypatch.delenv(missing)
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)
    assert missing.lower() in str(exc.value)


def test_empty_model_is_rejected(monkeypatch):
    _set_env(monkeypatch, LLM_MODEL="")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "name,value", [("LLM_TIMEOUT_SECONDS", "0"), ("RATE_LIMIT_PER_MINUTE", "0")]
)
def test_invalid_numbers_are_rejected(monkeypatch, name, value):
    _set_env(monkeypatch, **{name: value})
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_secrets_are_masked_in_repr_and_str(monkeypatch):
    _set_env(monkeypatch)
    s = Settings(_env_file=None)
    for rendered in (repr(s), str(s), s.model_dump_json()):
        assert "discord-secret" not in rendered
        assert "llm-secret" not in rendered


def test_main_reports_missing_config_without_leaking_secrets(monkeypatch, caplog):
    monkeypatch.setenv("LLM_API_KEY", "llm-secret")
    monkeypatch.chdir(Path(__file__).parent)  # ensure no stray .env is picked up
    assert main() == 1
    assert "DISCORD_TOKEN" in caplog.text
    assert "llm-secret" not in caplog.text


def test_main_runs_bot_with_valid_config(monkeypatch, caplog):
    _set_env(monkeypatch, DISCORD_GUILD_ID="123")
    monkeypatch.chdir(Path(__file__).parent)
    seen = {}

    async def fake_run(settings):
        seen["settings"] = settings

    monkeypatch.setattr(entry, "run", fake_run)
    assert main() == 0
    assert seen["settings"].discord_guild_id == 123
    assert "discord-secret" not in caplog.text
    assert "llm-secret" not in caplog.text


def test_main_reports_rejected_token(monkeypatch, caplog):
    _set_env(monkeypatch)
    monkeypatch.chdir(Path(__file__).parent)

    async def fake_run(settings):
        raise discord.LoginFailure("bad token")

    monkeypatch.setattr(entry, "run", fake_run)
    assert main() == 1
    assert "DISCORD_TOKEN" in caplog.text
    assert "discord-secret" not in caplog.text


@pytest.mark.parametrize("name", ["DISCORD_TOKEN", "LLM_API_KEY"])
def test_blank_secrets_are_rejected(monkeypatch, name):
    _set_env(monkeypatch, **{name: ""})
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize("name", ["DISCORD_GUILD_ID", "LLM_MAX_TOKENS"])
def test_blank_optional_ints_mean_unset(monkeypatch, name):
    _set_env(monkeypatch, **{name: ""})
    assert getattr(Settings(_env_file=None), name.lower()) is None


def test_optional_ints_parse(monkeypatch):
    _set_env(monkeypatch, DISCORD_GUILD_ID="123", LLM_MAX_TOKENS="800")
    s = Settings(_env_file=None)
    assert (s.discord_guild_id, s.llm_max_tokens) == (123, 800)


def test_game_settings(monkeypatch):
    _set_env(monkeypatch)
    s = Settings(_env_file=None)
    assert (s.game_round_hours, s.game_target_rounds) == (24.0, 12)
    _set_env(monkeypatch, GAME_ROUND_HOURS="0.5", GAME_TARGET_ROUNDS="6")
    s = Settings(_env_file=None)
    assert (s.game_round_hours, s.game_target_rounds) == (0.5, 6)
    _set_env(monkeypatch, GAME_TARGET_ROUNDS="2")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_numbered_bot_profiles(monkeypatch):
    _set_env(
        monkeypatch,
        DISCORD_TOKEN="legacy-token",
        LLM_API_KEY="legacy-key",
        LLM_MODEL="legacy-model",
        DISCORD_TOKEN_2="second-token",
        LLM_API_KEY_2="second-key",
        LLM_MODEL_2="second-model",
        LLM_BASE_URL_2="https://example.test/v1",
        DISCORD_GUILD_ID_2="456",
    )
    profiles = Settings(_env_file=None).bot_profiles()
    assert len(profiles) == 2
    assert profiles[0].number == 1
    assert profiles[1].discord_token.get_secret_value() == "second-token"
    assert profiles[1].llm_base_url == "https://example.test/v1"
    assert profiles[1].discord_guild_id == 456


def test_numbered_profile_one_can_replace_legacy_values(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN_1", "first-token")
    monkeypatch.setenv("LLM_API_KEY_1", "first-key")
    monkeypatch.setenv("LLM_MODEL_1", "first-model")
    settings = Settings(_env_file=None)
    assert settings.bot_profiles()[0].discord_token.get_secret_value() == "first-token"
