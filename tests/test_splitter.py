import random
import re
from collections import Counter

import pytest

from companion.splitter import DISCORD_LIMIT, split_message

# Independent of the implementation: in this test data, fences are always a
# whole line of ``` or ~~~ plus an optional language, and never nested.
_TEST_FENCE = re.compile(r"^\s*(```|~~~)[a-z]*$")


def fences_balanced(chunk: str) -> bool:
    open_marker = None
    for line in chunk.split("\n"):
        m = _TEST_FENCE.match(line)
        if not m:
            continue
        if open_marker is None:
            open_marker = m.group(1)
        elif m.group(1) == open_marker and line.strip() == open_marker:
            open_marker = None
    return open_marker is None


def tokens(text: str) -> Counter:
    return Counter(re.findall(r"\b[LW]\d+\b", text))


def test_short_message_is_unchanged():
    assert split_message("hello") == ["hello"]


def test_empty_and_whitespace_produce_no_chunks():
    assert split_message("") == []
    assert split_message("  \n\n ") == []


def test_exactly_at_limit_is_one_chunk():
    text = "a" * DISCORD_LIMIT
    assert split_message(text) == [text]


def test_prefers_paragraph_boundaries():
    para = ("word " * 50).strip()  # 249 chars
    text = "\n\n".join([para] * 3)
    assert split_message(text, limit=600) == ["\n\n".join([para] * 2), para]


def test_falls_back_to_word_boundaries():
    text = " ".join(["word"] * 100)  # 499 chars, no newlines
    chunks = split_message(text, limit=200)
    assert all(len(c) <= 200 for c in chunks)
    assert all(c == c.strip() for c in chunks)
    assert " ".join(chunks) == text


def test_hard_cuts_unbroken_text():
    text = "x" * 450
    chunks = split_message(text, limit=200)
    assert all(len(c) <= 200 for c in chunks)
    assert "".join(chunks) == text


def test_code_block_is_closed_and_reopened_with_language():
    code = "\n".join(f"print({i})" for i in range(60))
    text = f"Here you go:\n\n```python\n{code}\n```\n\nDone."
    chunks = split_message(text, limit=200)

    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)
    assert all(fences_balanced(c) for c in chunks)
    assert all(c.startswith("```python\n") for c in chunks[1:] if "print(" in c)
    joined = "\n".join(chunks)
    for i in range(60):
        assert joined.count(f"print({i})") == 1


def test_tilde_fences_are_tracked():
    code = "\n".join(f"line {i}" for i in range(60))
    chunks = split_message(f"~~~\n{code}\n~~~", limit=150)
    assert len(chunks) > 1
    assert all(fences_balanced(c) for c in chunks)
    assert all(c.startswith("~~~\n") for c in chunks)


def test_code_indentation_is_preserved():
    code = "\n".join(f"    line_{i} = {i}" for i in range(40))
    joined = "\n".join(split_message(f"```\n{code}\n```", limit=150))
    for i in range(40):
        assert f"    line_{i} = {i}" in joined


def test_single_line_inline_fence_is_not_a_code_block():
    # Regression: ```print(1)``` was treated as an opening fence and duplicated.
    text = "```print(1)```\n" + "word " * 40
    chunks = split_message(text, limit=100)
    joined = "\n".join(chunks)
    assert joined.count("print(1)") == 1
    assert not any(c.endswith("\n```") for c in chunks)


def test_long_language_tag_is_not_repeated():
    tag = "x" * 60
    text = f"```{tag}\n" + "\n".join(f"line {i}" for i in range(40)) + "\n```"
    chunks = split_message(text, limit=150)
    assert all(len(c) <= 150 for c in chunks)
    assert all(c.startswith("```\n") for c in chunks[1:])


def test_never_emits_whitespace_only_chunks():
    # Regression: a run of spaces produced a chunk of only spaces.
    chunks = split_message("a" * 60 + " " * 200 + "b", limit=100)
    assert chunks == ["a" * 60, "b"]


def test_limit_too_small_is_rejected():
    with pytest.raises(ValueError):
        split_message("hi", limit=10)


def _random_text(rng: random.Random) -> tuple[str, Counter]:
    """Prose and code blocks where every word/line carries a unique token."""
    parts, n = [], 0
    for _ in range(rng.randint(5, 40)):
        if rng.random() < 0.3:
            fence = rng.choice(["```", "~~~"])
            lang = rng.choice(["", "python", "js"])
            lines = []
            for _ in range(rng.randint(1, 30)):
                n += 1
                lines.append(" " * rng.randint(0, 8) + f"L{n} " + "x" * rng.randint(0, 70))
            parts.append(f"{fence}{lang}\n" + "\n".join(lines) + f"\n{fence}")
        else:
            words = []
            for _ in range(rng.randint(1, 80)):
                n += 1
                words.append(f"W{n}")
            parts.append(" ".join(words))
    text = "\n\n".join(parts)
    return text, tokens(text)


@pytest.mark.parametrize("seed", range(40))
def test_random_text_respects_limit_fences_and_content(seed):
    rng = random.Random(seed)
    text, expected = _random_text(rng)
    limit = rng.choice([150, 500, DISCORD_LIMIT])

    chunks = split_message(text, limit=limit)

    assert chunks
    assert all(0 < len(c) <= limit and c.strip() for c in chunks)
    assert all(fences_balanced(c) for c in chunks)
    if limit >= 500:  # no hard cuts possible, so every token survives intact, once
        assert tokens("\n".join(chunks)) == expected
