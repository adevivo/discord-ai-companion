import random

import pytest

from companion.splitter import DISCORD_LIMIT, split_message


def _fences_balanced(chunk: str) -> bool:
    return sum(1 for line in chunk.split("\n") if line.strip().startswith("```")) % 2 == 0


def test_short_message_is_unchanged():
    assert split_message("hello") == ["hello"]


def test_empty_and_whitespace_produce_no_chunks():
    assert split_message("") == []
    assert split_message("  \n\n ") == []


def test_exactly_at_limit_is_one_chunk():
    text = "a" * DISCORD_LIMIT
    assert split_message(text) == [text]


def test_prefers_paragraph_boundaries():
    para = "word " * 50  # ~250 chars
    text = "\n\n".join([para.strip()] * 3)
    chunks = split_message(text, limit=600)
    assert chunks == ["\n\n".join([para.strip()] * 2), para.strip()]


def test_falls_back_to_word_boundaries():
    text = " ".join(["word"] * 100)  # 499 chars, no newlines
    chunks = split_message(text, limit=200)
    assert all(len(c) <= 200 for c in chunks)
    assert all(not c.startswith(" ") and not c.endswith(" ") for c in chunks)
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
    assert all(_fences_balanced(c) for c in chunks)
    for chunk in chunks[1:]:
        if "print(" in chunk.split("\n")[1]:
            assert chunk.startswith("```python\n")
    # every line of code survives
    joined = "\n".join(chunks)
    for i in range(60):
        assert f"print({i})" in joined


def test_code_indentation_is_preserved():
    code = "\n".join(f"    line_{i} = {i}" for i in range(40))
    text = f"```\n{code}\n```"
    joined = "\n".join(split_message(text, limit=150))
    for i in range(40):
        assert f"    line_{i} = {i}" in joined


def test_limit_too_small_is_rejected():
    with pytest.raises(ValueError):
        split_message("hi", limit=10)


@pytest.mark.parametrize("seed", range(25))
def test_random_text_respects_limit_and_fences(seed):
    rng = random.Random(seed)
    parts = []
    for _ in range(rng.randint(5, 40)):
        if rng.random() < 0.3:
            lang = rng.choice(["", "python", "js"])
            lines = [
                " " * rng.randint(0, 8) + "x" * rng.randint(1, 80)
                for _ in range(rng.randint(1, 30))
            ]
            parts.append(f"```{lang}\n" + "\n".join(lines) + "\n```")
        else:
            parts.append(" ".join("w" * rng.randint(1, 12) for _ in range(rng.randint(1, 80))))
    text = "\n\n".join(parts)
    limit = rng.choice([150, 500, DISCORD_LIMIT])

    chunks = split_message(text, limit=limit)

    assert chunks
    assert all(0 < len(c) <= limit for c in chunks)
    assert all(_fences_balanced(c) for c in chunks)
