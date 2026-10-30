"""LLM client tests.

These run the real OpenAI SDK against an in-process fake HTTP transport, so
request formatting and error mapping are tested end to end without network.
"""

import json

import httpx2 as httpx
import openai
import pytest

from companion.llm import (
    ChatMessage,
    LLMClient,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
)

MESSAGES = [ChatMessage("system", "be nice"), ChatMessage("user", "hello")]


def _completion(content: str | None) -> dict:
    choices = (
        []
        if content is None
        else [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ]
    )
    return {
        "id": "x",
        "object": "chat.completion",
        "created": 0,
        "model": "test/model",
        "choices": choices,
    }


def _client(settings, handler) -> LLMClient:
    sdk = openai.AsyncOpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key.get_secret_value(),
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return LLMClient(settings, client=sdk)


async def test_sends_model_messages_and_auth(settings):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion("hi there"))

    reply = await _client(settings, handler).complete(MESSAGES)

    assert reply == "hi there"
    assert seen["url"] == "https://llm.test/v1/chat/completions"
    assert seen["auth"] == "Bearer llm-secret"
    assert seen["body"]["model"] == "test/model"
    assert seen["body"]["messages"] == [
        {"role": "system", "content": "be nice"},
        {"role": "user", "content": "hello"},
    ]


async def test_rate_limit_is_mapped(settings):
    client = _client(settings, lambda r: httpx.Response(429, json={"error": {"message": "slow"}}))
    with pytest.raises(LLMRateLimitError):
        await client.complete(MESSAGES)


@pytest.mark.parametrize("status", [400, 401, 500, 503])
async def test_http_errors_are_mapped(settings, status):
    client = _client(settings, lambda r: httpx.Response(status, json={"error": {"message": "x"}}))
    with pytest.raises(LLMError, match=str(status)) as exc:
        await client.complete(MESSAGES)
    assert not isinstance(exc.value, LLMRateLimitError)


async def test_timeout_is_mapped(settings):
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LLMTimeoutError):
        await _client(settings, handler).complete(MESSAGES)


async def test_connection_error_is_mapped(settings):
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(LLMError, match="connect"):
        await _client(settings, handler).complete(MESSAGES)


@pytest.mark.parametrize("content", [None, "", "   "])
async def test_empty_responses_are_errors(settings, content):
    client = _client(settings, lambda r: httpx.Response(200, json=_completion(content)))
    with pytest.raises(LLMError):
        await client.complete(MESSAGES)


async def test_api_key_not_in_error_messages(settings):
    client = _client(settings, lambda r: httpx.Response(401, json={"error": {"message": "bad"}}))
    with pytest.raises(LLMError) as exc:
        await client.complete(MESSAGES)
    assert "llm-secret" not in str(exc.value)


async def test_rejects_empty_message_list(settings):
    with pytest.raises(ValueError):
        await _client(settings, lambda r: httpx.Response(200)).complete([])
