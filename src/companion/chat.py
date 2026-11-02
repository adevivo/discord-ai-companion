"""Conversation logic, independent of Discord.

Builds the prompt from the system prompt plus recent context, calls the LLM,
and stores the exchange. Failures become friendly, user-facing messages; an
exchange is only stored when the LLM actually answered.
"""

import asyncio
import logging
import math
import time
from collections import defaultdict
from collections.abc import Callable

from companion.llm import ChatMessage, LLMClient, LLMError, LLMRateLimitError, LLMTimeoutError
from companion.ratelimit import RateLimiter
from companion.store import ContextStore

log = logging.getLogger(__name__)

MAX_PROMPT_CHARS = 4000
GLOBAL_KEY = "*"

SPEAKER_NOTE = (
    "\n\nMultiple people may talk to you in the same channel. "
    'Each user message is prefixed with the speaker\'s display name, e.g. "Sam: hello".'
)

# Kept short on purpose: the full command list lives behind /help, not in every prompt.
BOT_NOTE = (
    "\n\nYou are a Discord bot with slash commands. If someone asks what you can do or how "
    "to use you, tell them to run `/help`. Don't invent commands. If someone wants to play an "
    "RPG or go on an adventure, recommend Game Master mode: `/game new` in a channel starts "
    "one in its own thread (details in `/help`). Don't run an improvised game in chat."
)

EMPTY_PROMPT_REPLY = "You pinged me but didn't say anything. What can I help with?"
PROMPT_TOO_LONG_REPLY = (
    f"That message is too long for me. Please keep it under {MAX_PROMPT_CHARS} characters."
)
PROVIDER_BUSY_REPLY = "The AI provider is rate-limiting us right now. Please try again in a minute."
TIMEOUT_REPLY = "The AI took too long to answer. Please try again."
LLM_ERROR_REPLY = (
    "Something went wrong talking to the AI. Please try again later. "
    "If it keeps happening in this channel, `/reset` may help."
)


def user_rate_limited_reply(retry_after: float) -> str:
    return f"You're sending requests too quickly. Try again in {math.ceil(retry_after)}s."


def bot_busy_reply(retry_after: float) -> str:
    return f"I'm handling a lot of requests right now. Try again in {math.ceil(retry_after)}s."


class ChatService:
    def __init__(
        self,
        llm: LLMClient,
        store: ContextStore,
        user_limiter: RateLimiter,
        global_limiter: RateLimiter,
        system_prompt: str,
        context_messages: int,
        context_max_chars: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._llm = llm
        self._store = store
        self._user_limiter = user_limiter
        self._global_limiter = global_limiter
        self._system_prompt = system_prompt + SPEAKER_NOTE + BOT_NOTE
        self._context_messages = context_messages
        self._context_max_chars = context_max_chars
        self._clock = clock
        # Serialize requests per channel/thread so each one sees the previous exchange.
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        # When each user was last told they're rate-limited, to avoid reply spam.
        self._notified_until: dict[str, float] = {}

    async def respond(
        self,
        scope_id: str,
        user_id: str,
        author_name: str,
        text: str,
        *,
        suppress_repeat_notices: bool = True,
    ) -> str | None:
        """Return the reply to post, or None if the bot should stay silent."""
        text = text.strip()
        if not text:
            return EMPTY_PROMPT_REPLY
        if len(text) > MAX_PROMPT_CHARS:
            return PROMPT_TOO_LONG_REPLY

        notice = self._check_rate_limits(user_id, suppress_repeat_notices)
        if notice is not None:
            return notice or None

        user_content = f"{author_name}: {text}"
        async with self._locks[scope_id]:
            history = self._trim(await self._store.recent(scope_id, self._context_messages))
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

            try:
                await self._store.add_exchange(
                    scope_id, user_content, reply, retain=self._context_messages
                )
            except Exception:
                # The user still gets their answer; we just lose it from context.
                log.exception("Failed to store exchange (scope=%s)", scope_id)
        return reply

    async def reset(self, scope_id: str) -> int:
        async with self._locks[scope_id]:
            return await self._store.clear(scope_id)

    def _check_rate_limits(self, user_id: str, suppress_repeats: bool) -> str | None:
        """None if allowed (and recorded); else a notice, or "" to stay silent."""
        user_wait = self._user_limiter.retry_after(user_id)
        global_wait = self._global_limiter.retry_after(GLOBAL_KEY)
        if user_wait == 0 and global_wait == 0:
            self._user_limiter.record(user_id)
            self._global_limiter.record(GLOBAL_KEY)
            return None

        now = self._clock()
        if suppress_repeats and now < self._notified_until.get(user_id, 0):
            return ""
        wait = max(user_wait, global_wait)
        self._notified_until[user_id] = now + wait
        if user_wait > 0:
            return user_rate_limited_reply(user_wait)
        log.warning("Global rate limit reached")
        return bot_busy_reply(global_wait)

    def _trim(self, history: list[ChatMessage]) -> list[ChatMessage]:
        """Keep the newest messages within the char budget, starting on a user turn."""
        kept: list[ChatMessage] = []
        total = 0
        for message in reversed(history):
            total += len(message.content)
            if total > self._context_max_chars:
                break
            kept.append(message)
        kept.reverse()
        while kept and kept[0].role != "user":
            kept.pop(0)
        return kept
