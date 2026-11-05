"""Resolve the business system selected by the ticket and render escalations."""

from __future__ import annotations

import re
from urllib.parse import quote


def normalize_system(value: object) -> str:
    """Return a configured system key in the form used by persisted state.

    The config loader converts uppercase dictionary keys such as ``PDM`` to
    ``P_D_M``. Collapse that representation while preserving normal identifiers
    such as ``HR_SYSTEM``.
    """
    normalized = str(value or "").strip().upper()
    if re.fullmatch(r"[A-Z](?:_[A-Z])+", normalized):
        return normalized.replace("_", "")
    return normalized


def normalize_skill_group(value: object) -> str:
    """Normalize configured and runtime skill-group labels for exact matching."""
    compact = re.sub(r"\s+", "", str(value or "").strip())
    # The JSON loader expands all-uppercase dictionary keys such as TMS into
    # T_M_S. Collapse those separators for skill-group labels as well.
    compact = re.sub(r"(?<=[A-Za-z/])_(?=[A-Za-z])", "", compact)
    return compact.casefold().replace("（", "(").replace("）", ")")


def _field_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_field_text(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_field_text(item) for item in value)
    return ""


def _match_system(text: str, systems: tuple[str, ...]) -> str:
    if not text or not systems:
        return ""
    alternatives = "|".join(re.escape(system) for system in systems)
    match = re.search(rf"(?<![A-Z0-9])(?:{alternatives})(?![A-Z0-9])", text, re.IGNORECASE)
    return match.group(0).upper() if match else ""


def detect_system(ticket: dict, messages: list[dict], systems: list[str] | tuple[str, ...]) -> str:
    """Use the initial customer choice, then assignment metadata as a fallback.

    The bot's classifier lists every available system in one assistant message,
    so non-human messages are deliberately ignored. The earliest human message
    containing a configured system is the closest durable record of the choice.
    """
    normalized = tuple(
        sorted(
            {normalize_system(value) for value in systems if value},
            key=len,
            reverse=True,
        )
    )
    if not normalized:
        return ""

    assignment = _match_system(_field_text(ticket.get("actual_agent_group")), normalized)
    if assignment:
        return assignment

    human_messages = [message for message in messages if message.get("human") is True]
    for message in human_messages:
        if "人工客服" not in str(message.get("text") or ""):
            continue
        selected = _match_system(str(message.get("text") or ""), normalized)
        if selected:
            return selected
    for message in human_messages:
        selected = _match_system(str(message.get("text") or ""), normalized)
        if selected:
            return selected

    return _match_system(_field_text(ticket.get("hit_agent_groups")), normalized)


def detect_skill_group(ticket: dict, groups: list[str] | tuple[str, ...]) -> str:
    """Return the configured skill group currently assigned to the ticket."""
    configured = {
        normalize_skill_group(group): str(group)
        for group in groups
        if normalize_skill_group(group)
    }
    if not configured:
        return ""
    candidates = [ticket.get("actual_agent_group")]
    hit_groups = ticket.get("hit_agent_groups")
    if isinstance(hit_groups, list):
        candidates.extend(
            group.get("name") if isinstance(group, dict) else group for group in hit_groups
        )
    for candidate in candidates:
        if normalize_skill_group(candidate) in configured:
            return str(candidate).strip()
    return ""


def ticket_subject(guest: object, system: str) -> str:
    """Return a short, non-empty human label without leaking unrelated fields."""
    name = ""
    if isinstance(guest, dict):
        name = str(guest.get("name") or "").strip()
    name = " ".join(name.split())[:60] or "客户"
    return f"{name} 的 {system} 工单"


def ticket_group_link(chat_id: str) -> str:
    return "https://applink.feishu.cn/client/chat/open?openChatId=" + quote(chat_id, safe="")


def render_escalation(
    ticket_id: str,
    system: str,
    chat_id: str,
    interval_minutes: int,
    guest: object = None,
) -> str:
    subject = ticket_subject(guest, system)
    link = ticket_group_link(chat_id)
    return (
        "【升级提醒】\n"
        f"{subject}（{ticket_id}）经首次催办后仍未解决，现已升级。\n"
        f"原工单群：{link}\n"
        "请相关同学尽快处理。问题彻底解决前，"
        f"本群每 {interval_minutes} 分钟提醒一次。"
    )


__all__ = [
    "detect_skill_group",
    "detect_system",
    "normalize_skill_group",
    "normalize_system",
    "render_escalation",
    "ticket_group_link",
    "ticket_subject",
]
