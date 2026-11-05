"""One-shot image summaries for Helpdesk follow-up judgement."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

from vikingbot.utils.image_format import detect_image_format

_SUMMARY_PROMPT = """请按图片顺序总结这些服务台截图，供后续自动跟进判断使用。
每张图分别说明：页面/模块、用户正在做什么、可见的报错或提示原文、关键单号或字段。
最后用一句话概括用户当前遇到的问题。不要臆测图片外的信息。"""


def _data_uri(path: Path) -> str:
    data = path.read_bytes()
    image_format = detect_image_format(data)
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{image_format.mime_type};base64,{encoded}"


async def summarize_images(
    paths: list[str],
    *,
    provider: Any,
    model: str | None,
    context: str = "",
    timeout: float = 180.0,
) -> str:
    """Summarize all images from one message in a single provider call.

    The caller deliberately does not impose a byte-size limit: Feishu screenshots
    may legitimately be around 10 MB each.  Count/time limits are enforced by the
    caller's media window instead.
    """
    available = [Path(path) for path in paths if path and Path(path).is_file()]
    if not available:
        return ""
    if provider is None:
        raise RuntimeError("image summary provider unavailable")

    text = _SUMMARY_PROMPT
    if context.strip():
        text += f"\n\n用户随图片发送的文字：\n{context.strip()[:4000]}"
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for path in available:
        content.append({"type": "image_url", "image_url": {"url": _data_uri(path)}})

    response = await asyncio.wait_for(
        provider.chat(
            messages=[{"role": "user", "content": content}],
            tools=[],
            model=model,
            max_tokens=1200,
            temperature=0,
            thinking=False,
            reasoning_effort="none",
        ),
        timeout,
    )
    summary = (getattr(response, "content", None) or "").strip()
    if not summary:
        raise RuntimeError("image summary returned no content")
    return summary
