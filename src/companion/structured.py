"""Get validated JSON out of an LLM without relying on native tool calling.

The model is asked for a JSON object; we extract it (tolerating code fences
and surrounding chatter), validate it against a Pydantic model, and on failure
re-ask once or twice with the validation error so the model can correct itself.
"""

import logging
import re

from pydantic import BaseModel, ValidationError

from companion.llm import ChatMessage, LLMClient, LLMError

log = logging.getLogger(__name__)

_CODE_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_ERROR_FEEDBACK_MAX = 500
_ECHO_MAX = 6000


def parse_json_reply[T: BaseModel](text: str, model: type[T]) -> T:
    """Extract the JSON object from an LLM reply and validate it. Raises ValueError."""
    fenced = _CODE_FENCE.search(text)
    candidate = fenced.group(1) if fenced else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object found in the reply")
    return model.model_validate_json(candidate[start : end + 1])


async def complete_json[T: BaseModel](
    llm: LLMClient, messages: list[ChatMessage], model: type[T], attempts: int = 3
) -> T:
    """Ask the LLM for JSON matching `model`, retrying with feedback on invalid output."""
    conversation = list(messages)
    for attempt in range(1, attempts + 1):
        reply = await llm.complete(conversation)
        try:
            return parse_json_reply(reply, model)
        except (ValueError, ValidationError) as exc:  # ValidationError is a ValueError
            error = " ".join(str(exc).split())[:_ERROR_FEEDBACK_MAX]
            log.warning("Invalid JSON from LLM (attempt %d/%d): %s", attempt, attempts, error)
            conversation = [
                *messages,
                ChatMessage("assistant", reply[:_ECHO_MAX]),
                ChatMessage(
                    "user",
                    f"That response was not valid for the required JSON format: {error}\n"
                    "Reply again with ONLY the corrected JSON object, no other text.",
                ),
            ]
    raise LLMError(f"LLM did not return valid JSON after {attempts} attempts")
