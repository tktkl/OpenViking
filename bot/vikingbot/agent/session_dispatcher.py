"""Bounded execution of FIFO session queues on a shared AgentLoop."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable

from loguru import logger

from vikingbot.bus.events import InboundMessage
from vikingbot.config.schema import SessionKey


class SessionDispatcher:
    """Run one item per ready session before offering that session again.

    A fixed set of workers drains the queues. Pending messages stay in deques,
    and a busy session never occupies another worker waiting for a lock. The handler
    owns execution, persistence and publication before the session is scheduled again.
    """

    def __init__(
        self,
        max_concurrent_sessions: int,
        handler: Callable[[InboundMessage], Awaitable[None]],
    ) -> None:
        if max_concurrent_sessions < 1:
            raise ValueError("max_concurrent_sessions must be positive")
        self._limit = max_concurrent_sessions
        self._handler = handler
        # A key remains present while ready OR executing, even if its deque is empty.
        self._pending: dict[SessionKey, deque[InboundMessage]] = {}
        self._ready: asyncio.Queue[SessionKey] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._closed = False

    def start(self) -> None:
        if self._workers or self._closed:
            raise RuntimeError("Session dispatcher has already started or closed")
        self._workers = [
            asyncio.create_task(self._work(), name=f"session-worker-{index}")
            for index in range(self._limit)
        ]

    def submit(self, msg: InboundMessage) -> None:
        """Accept an item without creating a task or waiting for its session."""
        if not self._workers or self._closed:
            raise RuntimeError("Session dispatcher is not running")
        pending = self._pending.get(msg.session_key)
        if pending is None:
            pending = self._pending[msg.session_key] = deque()
            self._ready.put_nowait(msg.session_key)
        pending.append(msg)

    def count_pending(self, session_key: SessionKey) -> int:
        """Count queued user inputs using the same rules as MessageBus."""
        return sum(
            1
            for msg in self._pending.get(session_key, ())
            if msg.internal_handler is None and not msg.internal
        )

    async def join(self) -> None:
        """Wait for all currently accepted work, including final persistence."""
        await self._ready.join()

    async def _work(self) -> None:
        # A handler may consume cancellation while finishing its cleanup. In that
        # case close() must still stop this worker before another queue wait.
        while not self._closed:
            session_key = await self._ready.get()
            pending = self._pending[session_key]
            msg = pending.popleft()
            try:
                await self._handler(msg)
            except asyncio.CancelledError:
                if self._closed:
                    raise
                logger.warning("Session item cancelled for {}", session_key)
            except Exception:
                # AgentLoop handles per-message errors. Also survive failures in
                # its error publication so one broken item cannot lose a worker.
                logger.exception("Session dispatch failed for {}", session_key)
            finally:
                if pending and not self._closed:
                    self._ready.put_nowait(session_key)
                else:
                    del self._pending[session_key]
                self._ready.task_done()

    async def close(self) -> None:
        """Stop dispatching and await worker/handler cleanup before dependencies close."""
        self._closed = True
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._pending.clear()
        while not self._ready.empty():
            self._ready.get_nowait()
            self._ready.task_done()
