"""Recent media selection for conversation turns.

Images are stored as short-lived cache paths plus stable channel references in
the local session transcript.  History sent to the model remains text-only;
only the current turn receives the selected image paths.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from vikingbot.bus.events import InboundMessage
from vikingbot.config.schema import FeishuMediaWindowConfig
from vikingbot.session.manager import Session


def session_media_fields(message: InboundMessage) -> dict[str, Any]:
    """Return session-only fields for a user message.

    ``Session.get_history`` ignores these fields, so they do not enter the LLM
    history or OpenViking compaction.  They remain available for the next turn's
    bounded media window.
    """
    fields: dict[str, Any] = {}
    metadata = message.metadata if isinstance(message.metadata, dict) else {}
    message_id = metadata.get("message_id")
    if isinstance(message_id, str) and message_id:
        fields["message_id"] = message_id
    refs = message.media_refs or metadata.get("media_refs")
    if isinstance(refs, list) and refs:
        fields["media_refs"] = refs
    paths = [item for item in message.media if isinstance(item, str) and item]
    if paths:
        fields["media_paths"] = paths
    return fields


def _parse_timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except (OSError, OverflowError, ValueError):
        return None


def _ref_path(ref: Any) -> str | None:
    if isinstance(ref, str):
        return ref
    if isinstance(ref, dict):
        path = ref.get("cache_path") or ref.get("path")
        if isinstance(path, str) and path:
            return path
    return None


def _ref_key(ref: Any, path: str) -> str:
    if isinstance(ref, dict):
        message_id = str(ref.get("message_id") or "")
        image_key = str(ref.get("image_key") or "")
        if message_id or image_key:
            return f"{message_id}:{image_key}"
    return f"path:{path}"


def select_recent_media(
    session: Session,
    message: InboundMessage,
    config: FeishuMediaWindowConfig,
    *,
    now: float | None = None,
) -> list[str]:
    """Select current, replied-to and recent same-sender images for this turn."""
    current_paths: list[str] = [item for item in message.media if isinstance(item, str) and item]
    if not config.enabled:
        return list(dict.fromkeys(current_paths))

    now = now if now is not None else datetime.now().timestamp()
    cutoff = now - config.max_age_minutes * 60
    metadata = message.metadata if isinstance(message.metadata, dict) else {}
    current_id = str(metadata.get("message_id") or "")
    reply_to = str(metadata.get("reply_to") or "")
    selected: list[str] = []
    seen: set[str] = set()

    def add_refs(refs: list[Any], message_id: str = "") -> None:
        for ref in refs:
            path = _ref_path(ref)
            if not path:
                continue
            key = _ref_key(ref, path)
            if not key.startswith(f"{message_id}:"):
                key = f"{message_id}:{key}" if message_id else key
            if key in seen or path in selected:
                continue
            seen.add(key)
            selected.append(path)

    add_refs(message.media_refs)
    for path in current_paths:
        if path not in selected:
            selected.append(path)

    recent_messages = session.messages[-config.max_messages :]

    # A quoted parent is the strongest historical signal and may be older than
    # the normal recent-message window.
    if config.include_reply_parent and reply_to:
        for item in reversed(session.messages):
            if item.get("message_id") != reply_to:
                continue
            add_refs(item.get("media_refs") or [], reply_to)
            for path in item.get("media_paths") or []:
                if isinstance(path, str) and path and path not in selected:
                    selected.append(path)
            break

    for item in reversed(recent_messages):
        if item.get("role") != "user":
            continue
        message_id = str(item.get("message_id") or "")
        if current_id and message_id == current_id:
            continue
        if config.same_sender_only and item.get("sender_id") != message.sender_id:
            continue
        timestamp = _parse_timestamp(item.get("timestamp"))
        if timestamp is None or timestamp < cutoff:
            continue
        add_refs(item.get("media_refs") or [], message_id)
        for path in item.get("media_paths") or []:
            if isinstance(path, str) and path and path not in selected:
                selected.append(path)

    return selected[: config.max_images]
