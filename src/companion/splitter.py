"""Split long replies into Discord-sized messages.

Prefers breaking at paragraph, then line, then word boundaries. If a split
lands inside a ``` code block, the block is closed at the end of the chunk and
re-opened (with the same language tag) at the start of the next one, so every
chunk renders correctly on its own.
"""

DISCORD_LIMIT = 2000
MIN_LIMIT = 100

_FENCE = "```"
_FENCE_CLOSE = "\n```"
_MAX_REOPEN_LEN = 30
_SEPARATORS = ("\n\n", "\n", " ")


def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    if limit < MIN_LIMIT:
        raise ValueError(f"limit must be at least {MIN_LIMIT}")

    remaining = text.strip()
    chunks: list[str] = []
    reopen = ""  # fence opener carried into the next chunk, e.g. "```python\n"

    while remaining:
        if len(reopen) + len(remaining) <= limit:
            chunks.append(reopen + remaining)
            break

        body_limit = limit - len(reopen) - len(_FENCE_CLOSE)
        piece, remaining = _cut(remaining, body_limit)
        chunk = reopen + piece

        opener = _open_fence(chunk)
        if opener is None:
            reopen = ""
        else:
            chunk += _FENCE_CLOSE
            reopen = (opener if len(opener) <= _MAX_REOPEN_LEN else _FENCE) + "\n"
        chunks.append(chunk)

    return chunks


def _cut(text: str, max_len: int) -> tuple[str, str]:
    """Return (piece, rest) with len(piece) <= max_len, preferring natural boundaries."""
    window = text[: max_len + 1]
    min_len = max_len // 2  # don't accept a boundary that leaves a tiny chunk
    for sep in _SEPARATORS:
        idx = window.rfind(sep, 0, max_len + 1)
        if idx >= min_len:
            return text[:idx], text[idx + len(sep) :]
    return text[:max_len], text[max_len:]


def _open_fence(chunk: str) -> str | None:
    """If chunk ends inside a code block, return that block's opening fence line."""
    opener: str | None = None
    for line in chunk.split("\n"):
        stripped = line.strip()
        if stripped.startswith(_FENCE):
            opener = stripped if opener is None else None
    return opener
