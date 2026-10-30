"""Conversation logic, independent of Discord.

Builds the prompt from the system prompt plus recent context, calls the LLM,
and stores the exchange. Failures become friendly, user-facing messages; an
exchange is only stored when the LLM actually answered.
"""

import logging
import math

from companion.llm import ChatMessage, LLMClient, LLMError, LLMRateLimitError, LLMTimeoutError
from companion.ratelimit import RateLimiter
from companion.store import ContextStore

log = logging.getLogger(__name__)

MAX_PROMPT_CHARS = 4000

SPEAKER_NOTE = (
    "\n\nMultiple people may talk to you in the same channel. "
    'Each user message is prefixed with the speaker\'s display name, e.g. "Sam: hello".'
)

EMPTY_PROMPT_REPLY = "You pinged me but didn't say anything. What can I help with?"
PROMPT_TOO_LONG_REPLY = (
    f"That message is too long for me. Please keep it under {MAX_PROMPT_CHARS} characters."
)
PROVIDER_BUSY_REPLY = "The AI provider is rate-limiting us right now. Please try again in a minute."
TIMEOUT_REPLY = "The AI took too long to answer. Please try again."
LLM_ERROR_REPLY = "Something went wrong talking to the AI. Please try again later."


def user_rate_limited_reply(retry_after: float) -> str:
    return f"You're sending requests too quickly. Try again in {math.ceil(retry_after)}s."


class ChatService:
    def __init__(
        self,
        llm: LLMClient,
        store: ContextStore,
        limiter: RateLimiter,
        system_prompt: str,
        context_messages: int,
    ) -> None:
        self._llm = llm
        self._store = store
        self._limiter = limiter
        self._system_prompt = system_prompt + SPEAKER_NOTE
        self._context_messages = context_messages

    async def respond(self, scope_id: str, user_id: str, author_name: str, text: str) -> str:
        text = text.strip()
        if not text:
            return EMPTY_PROMPT_REPLY
        if len(text) > MAX_PROMPT_CHARS:
            return PROMPT_TOO_LONG_REPLY

        retry_after = self._limiter.try_acquire(user_id)
        if retry_after > 0:
            return user_rate_limited_reply(retry_after)

        user_content = f"{author_name}: {text}"
        history = await self._store.recent(scope_id, self._context_messages)
        messages = [
            ChatMessage("system", self._system_prompt),
            *history,
            ChatMessage("user", user_content),
        ]

        try:
            reply = await self._llm.complete(messages)
        except LLMRateLimitError:
            log.warning("LLM provider rate limit hit (scope=%s)", scope_id)
            return PROVIDER_BUSY_REPLY
        except LLMTimeoutError:
            log.warning("LLM request timed out (scope=%s)", scope_id)
            return TIMEOUT_REPLY
        except LLMError as exc:
            log.error("LLM request failed (scope=%s): %s", scope_id, exc)
            return LLM_ERROR_REPLY

        await self._store.add_exchange(scope_id, user_content, reply, retain=self._context_messages)
        return reply
