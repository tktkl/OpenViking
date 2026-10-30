"""Join Helpdesk ticket chats, then use the upstream Feishu IM implementation unchanged."""

import asyncio
from collections import OrderedDict
from functools import partial

import httpx
from loguru import logger

from vikingbot.channels import feishu
from vikingbot.channels.feishu import FeishuChannel
from vikingbot.extensions.feishu_helpdesk.api import HelpdeskAPI, HelpdeskAPIError
from vikingbot.extensions.feishu_helpdesk.cli import LarkCLIError

CLOSED_STATUSES = {50, 51, 52}
ADMISSION_RETRY_DELAYS = (2, 5, 10, 30, 60)


def _error_details(error: Exception) -> str:
    # Never render arbitrary exception messages, request URLs or response bodies.
    status = (
        error.response.status_code
        if isinstance(error, httpx.HTTPStatusError)
        else getattr(error, "status", 0)
    )
    return f"error={type(error).__name__}, code={getattr(error, 'code', None)}, HTTP={status}"


class FeishuHelpdeskChannel(FeishuChannel):
    def __init__(self, config, bus, *, bot_config=None, **kwargs):
        super().__init__(config, bus, bot_config=bot_config, **kwargs)
        self._helpdesk_api = HelpdeskAPI(
            config.helpdesk,
            app_id=config.app_id,
            domain=config.domain,
            tenant_token=self._get_tenant_access_token,
        )
        self._helpdesk_tasks: set[asyncio.Task] = set()
        self._helpdesk_ready = False
        self._join_tasks: dict[str, asyncio.Task] = {}
        self._ticket_chats: OrderedDict[str, str] = OrderedDict()

    def _event_handler_builder(self):
        return (
            super()
            ._event_handler_builder()
            .register_p2_helpdesk_ticket_created_v1(self._on_ticket_sync)
            .register_p2_helpdesk_ticket_updated_v1(self._on_ticket_sync)
        )

    async def start(self):
        try:
            if not feishu.FEISHU_AVAILABLE:
                raise RuntimeError("Feishu Helpdesk requires the lark-oapi SDK")
            if not self.config.app_id or not self.config.app_secret:
                raise ValueError("Feishu Helpdesk requires app_id and app_secret")
            # Native IM must not wait for or depend on Helpdesk APIs. The task
            # starts after super().start() yields with the IM connection running.
            task = asyncio.create_task(self._initialize_helpdesk())
            self._helpdesk_tasks.add(task)
            task.add_done_callback(self._ticket_done)
            await super().start()
        finally:
            await self.stop()

    async def _initialize_helpdesk(self):
        try:
            self.bot_open_id = await self._helpdesk_api.bot_open_id()
            await self._helpdesk_api.subscribe()
            self._helpdesk_api.start()
            self._helpdesk_ready = True
            logger.info("Feishu Helpdesk ready")
        except Exception as error:
            # Keep native IM available, but do not admit ticket events without a
            # successful Helpdesk initialization. Never log response bodies/tokens.
            logger.error(
                "Feishu Helpdesk unavailable; native IM remains active: {}",
                type(error).__name__,
            )

    async def stop(self):
        await super().stop()
        await self._stop_helpdesk()

    async def _stop_helpdesk(self):
        self._running = False
        self._helpdesk_ready = False
        tasks = self._helpdesk_tasks | set(self._join_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._helpdesk_tasks.clear()
        self._join_tasks.clear()
        self._ticket_chats.clear()
        await self._helpdesk_api.close()

    def _on_ticket_sync(self, data):
        # The SDK dispatches from its WebSocket thread; all state belongs to the bot loop.
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._schedule_ticket, data)

    def _schedule_ticket(self, data):
        if not self._running or not self._helpdesk_ready:
            return
        task = asyncio.create_task(self._on_ticket(data))
        self._helpdesk_tasks.add(task)
        task.add_done_callback(partial(self._ticket_done, data=data))

    def _ticket_done(self, task, *, data=None):
        self._helpdesk_tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            event = getattr(data, "event", None)
            ticket = getattr(event, "object", event)
            logger.error(
                "Feishu Helpdesk event failed: ticket_id={}, chat_id={}, {}",
                getattr(ticket, "ticket_id", None),
                getattr(ticket, "chat_id", None),
                _error_details(error),
            )

    async def _ensure_in_chat_with_retry(self, chat_id: str, *, ticket_id: str) -> bool:
        # SDK callbacks acknowledge events before admission finishes. Keep transient
        # failures in the same shared task so retries do not depend on another event.
        for attempt in range(len(ADMISSION_RETRY_DELAYS) + 1):
            try:
                # Check membership again before inviting: a timed-out invite may have succeeded.
                return await self._helpdesk_api.ensure_in_chat(chat_id)
            except (
                httpx.TimeoutException,
                httpx.NetworkError,
                httpx.RemoteProtocolError,
                httpx.HTTPStatusError,
                HelpdeskAPIError,
                LarkCLIError,
            ) as error:
                if isinstance(error, LarkCLIError):
                    # A newly created ticket chat may not contain the inviting user yet.
                    # Keep this exception local to admission, not CLI auth or keepalive.
                    retryable = not error.authentication and (
                        error.retryable or error.code == 232011
                    )
                elif isinstance(error, HelpdeskAPIError):
                    retryable = error.retryable
                elif isinstance(error, httpx.HTTPStatusError):
                    status = error.response.status_code
                    retryable = status in (408, 429) or 500 <= status < 600
                else:
                    retryable = True
                if not retryable or attempt == len(ADMISSION_RETRY_DELAYS):
                    raise
                delay = ADMISSION_RETRY_DELAYS[attempt]
                logger.warning(
                    "Feishu Helpdesk admission retry: ticket_id={}, chat_id={}, "
                    "attempt={}, delay={}s, {}",
                    ticket_id,
                    chat_id,
                    attempt + 2,
                    delay,
                    _error_details(error),
                )
                # Cancellation from ticket closure or shutdown must interrupt the backoff.
                await asyncio.sleep(delay)

    async def _on_ticket(self, data):
        header = data.header
        if header.app_id and header.app_id != self.config.app_id:
            return
        if header.event_type == "helpdesk.ticket.created_v1":
            ticket = data.event
        elif header.event_type == "helpdesk.ticket.updated_v1":
            ticket = data.event.object
        else:
            return
        if str(ticket.helpdesk_id or "") != self.config.helpdesk.helpdesk_id:
            return
        chat_id = ticket.chat_id or ""
        ticket_id = str(ticket.ticket_id or "")
        if not chat_id.startswith("oc_") or not ticket_id:
            # Creation can precede group creation; updated_v1 supplies its eventual chat_id.
            return
        if ticket.status in CLOSED_STATUSES:
            pending = self._join_tasks.pop(chat_id, None)
            if pending is not None:
                pending.cancel()
            self._ticket_chats.pop(chat_id, None)
            return
        if ticket.status not in {1, 2, 3, 4, 5}:
            return
        task = self._join_tasks.get(chat_id)
        if task is None:
            task = asyncio.create_task(
                self._ensure_in_chat_with_retry(chat_id, ticket_id=ticket_id)
            )
            self._join_tasks[chat_id] = task
        try:
            if not await asyncio.shield(task):
                return
            # Closure/shutdown may remove the completed join before this waiter
            # resumes. Only its current owner may establish a ticket binding.
            if self._join_tasks.get(chat_id) is not task:
                return
        finally:
            if task.done() and self._join_tasks.get(chat_id) is task:
                self._join_tasks.pop(chat_id, None)
        self._ticket_chats[chat_id] = ticket_id
        self._ticket_chats.move_to_end(chat_id)
        while len(self._ticket_chats) > 1000:
            self._ticket_chats.popitem(last=False)
        logger.info("Feishu Helpdesk bot admitted: ticket_id={}, chat_id={}", ticket_id, chat_id)

    async def _handle_message(self, *args, metadata=None, **kwargs):
        # Keep sender IDs, permission checks, media, topics and reply anchors from upstream.
        metadata = dict(metadata or {})
        if ticket_id := self._ticket_chats.get(metadata.get("reply_to")):
            metadata["helpdesk_ticket_id"] = ticket_id
            metadata["helpdesk_id"] = self.config.helpdesk.helpdesk_id
        return await super()._handle_message(*args, metadata=metadata, **kwargs)
