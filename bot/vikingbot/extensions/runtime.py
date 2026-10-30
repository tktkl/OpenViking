"""Small, vendor-neutral lifecycle seam for optional gateway extensions.

Factories are trusted deployment configuration, not model-controlled imports.
Only the main AgentLoop loads them: compile/subagent tool factories stay unchanged.
An external Python package can use the same interface without patching the loop.
"""

from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

from vikingbot.agent.tools.registry import ToolRegistry
from vikingbot.bus.queue import MessageBus
from vikingbot.config.schema import Config
from vikingbot.sandbox.manager import SandboxManager
from vikingbot.session.manager import SessionManager


@dataclass(frozen=True)
class RuntimeContext:
    """Host services available to an extension; credentials are not serialized."""

    config: Config
    workspace: Path
    bus: MessageBus
    tools: ToolRegistry
    sessions: SessionManager
    sandbox_manager: SandboxManager | None


class RuntimeExtensions:
    """Load once, start with the live bus loop, close before sandbox teardown."""

    def __init__(self, context: RuntimeContext):
        self.extensions: list[Any] = []
        for spec in getattr(context.config, "runtime_extensions", ()):
            module, separator, name = spec.factory.partition(":")
            if not separator or not module or not name:
                raise ValueError("Runtime extension factory must be module:callable")
            extension = getattr(import_module(module), name)(context, spec.options)
            self.extensions.append(extension)

    async def start(self) -> None:
        try:
            for extension in self.extensions:
                await extension.start()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        for extension in reversed(self.extensions):
            await extension.close()
