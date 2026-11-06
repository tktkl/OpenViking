"""Send one workspace file as a native attachment to the current chat."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from vikingbot.agent.tools.base import Tool
from vikingbot.bus.events import OutboundMessage

if TYPE_CHECKING:
    from vikingbot.agent.tools.base import ToolContext

_DRIVE_PATH = re.compile(r"^[A-Za-z]:")


class SendFileTool(Tool):
    """Queue a current-workspace file for native channel delivery."""

    def __init__(self, send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None):
        self._send_callback = send_callback

    @property
    def name(self) -> str:
        return "send_file"

    @property
    def description(self) -> str:
        return (
            "Send a file from the current session workspace to the current Feishu chat as a "
            "native file attachment. Use a relative path such as work/report.xlsx."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative path to the file, for example work/report.xlsx",
                },
            },
            "required": ["path"],
        }

    async def execute(
        self,
        tool_context: "ToolContext",
        path: str,
        **kwargs: Any,
    ) -> str:
        if not self._send_callback:
            return "Error: File sending not configured"
        if tool_context.session_key is None or tool_context.session_key.type != "feishu":
            return "Error: File sending is currently supported only for Feishu chats"
        if tool_context.sandbox_manager is None:
            return "Error: Session workspace is unavailable"

        relative = str(path or "").strip()
        pure = PurePosixPath(relative)
        if (
            not relative
            or "\x00" in relative
            or relative.startswith(("/", "\\"))
            or _DRIVE_PATH.match(relative)
            or ".." in pure.parts
        ):
            return "Error: path must be a relative path inside the session workspace"

        try:
            sandbox = await tool_context.sandbox_manager.get_sandbox(tool_context.session_key)
            local_path = (
                sandbox.local_file_path(relative) if hasattr(sandbox, "local_file_path") else None
            )
            if local_path is not None:
                workspace = getattr(sandbox, "workspace", None)
                if workspace is not None and not local_path.resolve().is_relative_to(
                    workspace.resolve()
                ):
                    return "Error: path escapes the session workspace"
                if not local_path.is_file():
                    return f"Error: file not found: {relative}"
                filename = local_path.name
            else:
                filename = pure.name

            content = await sandbox.read_file_bytes(relative)
        except FileNotFoundError:
            return f"Error: file not found: {relative}"
        except (OSError, ValueError) as exc:
            return f"Error reading file: {exc}"
        except Exception as exc:
            return f"Error sending file: {type(exc).__name__}"

        if not content:
            return "Error: refusing to send an empty file"
        outbound = OutboundMessage(
            session_key=tool_context.session_key,
            content="",
            files=[
                {
                    "filename": filename,
                    "content": content,
                }
            ],
            metadata=dict(getattr(tool_context, "channel_metadata", None) or {}),
        )
        try:
            await self._send_callback(outbound)
        except Exception as exc:
            return f"Error sending file: {type(exc).__name__}"
        return f"File send queued: {filename} ({len(content)} bytes)"
