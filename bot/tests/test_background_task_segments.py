"""Provider-neutral result splitting: no code execution or network required."""

import pytest
from vikingbot.extensions.background_tasks.segments import (
    encoded_size,
    numbered_parts,
    split_markdown,
)


@pytest.mark.parametrize("budget", [1024, 4096, 8192])
@pytest.mark.parametrize("text", [
    "", "短答复", "🙂引号\"与反斜杠\\\n" * 6000, "x" * 300000,
    "段落\n\n" * 3000, "```python\n" + "print('🙂')\n" * 3000 + "```\n结论",
    "~~~sql\n" + "select 1;\r\n" * 3000 + "~~~\n",
    "开头\n" + "\n" * 10000, "```\n" + "a" * 10000,
])
def test_segmentation_preserves_exact_source_and_encoded_budget(text, budget):
    segments = split_markdown(text, budget)
    assert "".join(part.source for part in segments) == text
    messages = numbered_parts(text, "a" * 64, "b" * 64, budget)
    assert len(messages) == len(segments)
    assert all(encoded_size(message) <= budget for message in messages)
    if len(segments) > 1:
        assert all(f"第 {i}/{len(messages)} 段" in value for i, value in enumerate(messages, 1))


def test_code_fences_are_reopened_and_closed_per_message():
    text = "```python\n" + "print('hello')\n" * 1000 + "```\n"
    segments = split_markdown(text, 1024)
    assert len(segments) > 2
    for segment in segments:
        assert segment.rendered.startswith("```python\n")
        assert segment.rendered.rstrip().endswith("```")
        assert segment.rendered.count("```") == 2


@pytest.mark.parametrize("budget", [0, 1023, 8193])
def test_invalid_segment_budgets_are_rejected(budget):
    with pytest.raises(ValueError):
        split_markdown("text", budget)
