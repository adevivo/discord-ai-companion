"""Thin wrapper around the OpenAI SDK for any OpenAI-compatible chat endpoint.

Only basic chat completions are used, so the bot stays portable across
OpenRouter, Ollama, vLLM, LM Studio, etc. SDK exceptions are translated into
our own small hierarchy so callers never depend on the SDK directly.
"""

import logging
from dataclasses import dataclass
from typing import Literal

import openai

from companion.config import Settings

log = logging.getLogger(__name__)

Role = Literal["system", "user", "assistant"]

TRUNCATION_NOTE = "\n\n*(reply truncated)*"
_PROVIDER_MESSAGE_MAX = 300


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str


class LLMError(Exception):
    """The LLM request failed or returned an unusable response."""


class LLMRateLimitError(LLMError):
    """The provider rejected the request due to rate limiting (HTTP 429)."""


class LLMTimeoutError(LLMError):
    """The request did not complete within the configured timeout."""


def _provider_message(exc: openai.APIStatusError) -> str:
    """The provider's error text (never headers), shortened for logs."""
    return " ".join(str(exc.message).split())[:_PROVIDER_MESSAGE_MAX]


class LLMClient:
    def __init__(self, settings: Settings, client: openai.AsyncOpenAI | None = None) -> None:
        self._model = settings.llm_model
        self._max_tokens = settings.llm_max_tokens
        self._client = client or openai.AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key.get_secret_value(),
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )

    async def aclose(self) -> None:
        await self._client.close()

    async def complete(self, messages: list[ChatMessage]) -> str:
        if not messages:
            raise ValueError("messages must not be empty")

        params: dict = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        if self._max_tokens is not None:
            params["max_tokens"] = self._max_tokens

        try:
            response = await self._client.chat.completions.create(**params)
        except openai.RateLimitError as exc:
            raise LLMRateLimitError("LLM provider rate limit reached") from exc
        except openai.APITimeoutError as exc:
            raise LLMTimeoutError("LLM request timed out") from exc
        except openai.APIStatusError as exc:
            raise LLMError(
                f"LLM provider returned HTTP {exc.status_code}: {_provider_message(exc)}"
            ) from exc
        except openai.APIConnectionError as exc:
            raise LLMError("Could not connect to LLM provider") from exc
        except openai.APIError as exc:
            raise LLMError(f"LLM response was invalid: {type(exc).__name__}") from exc
        except ValueError as exc:  # e.g. a non-JSON body from a misconfigured base URL
            raise LLMError("LLM response was not valid JSON (check LLM_BASE_URL)") from exc

        return self._extract_content(response)

    @staticmethod
    def _extract_content(response: object) -> str:
        # Defensive: a wrong base URL can yield an HTML string, and providers
        # sometimes return 200 with no choices or a null message.
        try:
            choice = response.choices[0]  # type: ignore[attr-defined]
            content = choice.message.content
            finish_reason = choice.finish_reason
        except (AttributeError, IndexError, TypeError) as exc:
            raise LLMError("LLM response was malformed (check LLM_BASE_URL)") from exc

        if not isinstance(content, str) or not content.strip():
            raise LLMError(f"LLM response was empty (finish_reason={finish_reason})")
        if finish_reason == "length":
            log.warning("LLM reply hit the token limit and was truncated")
            return content + TRUNCATION_NOTE
        if finish_reason == "content_filter":
            log.warning("LLM reply was cut short by the provider's content filter")
        return content
