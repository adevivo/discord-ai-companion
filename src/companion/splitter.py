"""Split long replies into Discord-sized messages.

Prefers breaking at paragraph, then line, then word boundaries. If a split
lands inside a fenced code block (``` or ~~~), the block is closed at the end
of the chunk and re-opened (with the same language tag) at the start of the
next one, so every chunk renders correctly on its own. Whitespace-only chunks
are never produced (Discord rejects empty messages).
"""

import re
from dataclasses import dataclass

DISCORD_LIMIT = 2000
MIN_LIMIT = 100

# A fence line is only fence characters plus an optional info string (language).
# Lines like ```print(1)``` are inline code, not fences.
_FENCE_LINE = re.compile(r"^\s*(`{3,20}|~{3,20})\s*([^`\s]*)\s*$")
_MAX_REOPEN_LEN = 30
_SEPARATORS = ("\n\n", "\n", " ")


@dataclass(frozen=True)
class _OpenFence:
    marker: str  # e.g. "```" or "~~~~"
    line: str  # the full opening line, e.g. "```python"

    @property
    def reopen(self) -> str:
        return (self.line if len(self.line) <= _MAX_REOPEN_LEN else self.marker) + "\n"

    @property
    def close(self) -> str:
        return "\n" + self.marker


def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    if limit < MIN_LIMIT:
        raise ValueError(f"limit must be at least {MIN_LIMIT}")

    remaining = text.strip()
    chunks: list[str] = []
    reopen = ""  # fence opener carried into the next chunk

    while remaining.strip():
        if len(reopen) + len(remaining) <= limit:
            chunks.append(reopen + remaining)
            break

        reserve = len("\n```")
        while True:
            piece, rest = _cut(remaining, limit - len(reopen) - reserve)
            fence = _open_fence(reopen + piece)
            close = fence.close if fence else ""
            if len(reopen) + len(piece) + len(close) <= limit:
                break
            reserve = len(close)  # a longer fence marker than assumed; cut again

        remaining = rest
        if not piece.strip():
            continue  # nothing visible in this stretch; don't emit an empty message
        chunks.append(reopen + piece + close)
        reopen = fence.reopen if fence else ""

    return chunks


def _cut(text: str, max_len: int) -> tuple[str, str]:
    """Return (piece, rest) with len(piece) <= max_len, preferring natural boundaries."""
    window = text[: max_len + 1]
    min_len = max_len // 2  # don't accept a boundary that leaves a tiny chunk
    for sep in _SEPARATORS:
        idx = window.rfind(sep, 0, max_len + 1)
        if idx >= min_len:
            piece, rest = text[:idx], text[idx + len(sep) :]
            if sep == " ":
                # Mid-line cut: surrounding spaces are not indentation.
                return piece.rstrip(" "), rest.lstrip(" ")
            # Line cut: keep the next line's indentation, drop blank lines.
            return piece.rstrip(), rest.lstrip("\n")
    return text[:max_len], text[max_len:]


def _open_fence(chunk: str) -> _OpenFence | None:
    """If chunk ends inside a fenced code block, describe that block's opening fence."""
    fence: _OpenFence | None = None
    for line in chunk.split("\n"):
        match = _FENCE_LINE.match(line)
        if not match:
            continue
        marker, info = match.groups()
        if fence is None:
            fence = _OpenFence(marker=marker, line=line.strip())
        elif not info and marker[0] == fence.marker[0] and len(marker) >= len(fence.marker):
            fence = None  # a matching closing fence
    return fence
