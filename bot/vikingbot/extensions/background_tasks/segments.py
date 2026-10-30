"""Deterministic Markdown segmentation, independent of any chat provider.

Budgets measure escaped UTF-8 JSON strings, including a numbered task header.
Paragraph/line boundaries are preferred; oversized lines split on Unicode
character boundaries. Code fences close/reopen for each rendered message, while
``source`` retains the exact original bytes for tests and the full result spool.
"""

import json
import re
from dataclasses import dataclass

FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)$")


def encoded_size(text: str) -> int:
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8"))


@dataclass(frozen=True)
class Segment:
    source: str
    rendered: str


def split_markdown(text: str, budget: int = 4096) -> list[Segment]:
    """Reserve space for task/run IDs, N/M numbering and synthetic fences."""
    if not 1024 <= budget <= 8192:
        raise ValueError("segment_bytes must be 1024..8192")
    body_budget = budget - 640
    parts = []
    source = ""
    used = 0
    fence = None
    opening = ""

    def flush():
        nonlocal source, used, opening
        if source:
            closing = "\n" + fence[0] if fence else ""
            parts.append(Segment(source, opening + source + closing))
            source, used = "", 0
            opening = fence[0] + fence[1] + "\n" if fence else ""

    for line in text.splitlines(keepends=True):
        offset = 0
        while offset < len(line):
            # Inspect at most one part worth of characters. Re-encoding the
            # entire remaining line on every iteration would be quadratic.
            candidate = line[offset:offset + body_budget]
            cost = encoded_size(candidate) - 2
            if cost <= body_budget - used:
                source += candidate
                used += cost
                offset += len(candidate)
            elif source:
                flush()
            else:
                low, high = 1, len(candidate)
                while low < high:
                    mid = (low + high + 1) // 2
                    if encoded_size(candidate[:mid]) - 2 <= body_budget:
                        low = mid
                    else:
                        high = mid - 1
                source = candidate[:low]
                offset += low
                used = encoded_size(source) - 2
                if offset < len(line):
                    flush()
        match = FENCE.fullmatch(line.rstrip("\r\n"))
        if match:
            marker, info = match.groups()
            if fence and marker[0] == fence[0][0] and len(marker) >= len(fence[0]) and not info.strip():
                fence = None
            elif fence is None and len(marker) <= 32 and encoded_size(marker + info) <= 128:
                fence = (marker, info)
        if not line.strip() and used > body_budget * 0.75:
            flush()
    flush()
    return parts or [Segment("", "")]


def numbered_parts(text: str, task_id: str, run_id: str, budget: int) -> list[str]:
    parts = split_markdown(text, budget)
    output = []
    for index, part in enumerate(parts, 1):
        header = f"后台任务 {task_id} / {run_id}"
        if len(parts) > 1:
            header += f"（第 {index}/{len(parts)} 段）"
        value = header + "\n\n" + part.rendered
        if encoded_size(value) > budget:
            raise ValueError("Segment header/fences exceed the configured message budget")
        output.append(value)
    return output
