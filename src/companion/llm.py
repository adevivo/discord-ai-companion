"""Thin wrapper around the OpenAI SDK for any OpenAI-compatible chat endpoint.

Only basic chat completions are used, so the bot stays portable across
OpenRouter, Ollama, vLLM, LM Studio, etc. SDK exceptions are translated into
our own small hierarchy so callers never depend on the SDK directly.
"""

from dataclasses import dataclass
from typing import Literal

import openai

from companion.config import Settings

Role = Literal["system", "user", "assistant"]


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


class LLMClient:
    def __init__(self, settings: Settings, client: openai.AsyncOpenAI | None = None) -> None:
        self._model = settings.llm_model
        self._client = client or openai.AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key.get_secret_value(),
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )

    async def complete(self, messages: list[ChatMessage]) -> str:
        if not messages:
            raise ValueError("messages must not be empty")

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": m.role, "content": m.content} for m in messages],
            )
        except openai.RateLimitError as exc:
            raise LLMRateLimitError("LLM provider rate limit reached") from exc
        except openai.APITimeoutError as exc:
            raise LLMTimeoutError("LLM request timed out") from exc
        except openai.APIStatusError as exc:
            raise LLMError(f"LLM provider returned HTTP {exc.status_code}") from exc
        except openai.APIConnectionError as exc:
            raise LLMError("Could not connect to LLM provider") from exc

        # Some providers return 200 with no choices (e.g. upstream model errors).
        if not response.choices:
            raise LLMError("LLM response contained no choices")
        content = response.choices[0].message.content
        if not content or not content.strip():
            raise LLMError("LLM response was empty")
        return content
