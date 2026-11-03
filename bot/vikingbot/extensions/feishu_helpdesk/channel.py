"""Join Helpdesk ticket chats, then use the upstream Feishu IM implementation unchanged."""

import asyncio
import time
from collections import OrderedDict
from functools import partial

import httpx
from loguru import logger

from vikingbot.channels import feishu
from vikingbot.channels.feishu import FeishuChannel
from vikingbot.extensions.feishu_helpdesk.api import HelpdeskAPI, HelpdeskAPIError
from vikingbot.extensions.feishu_helpdesk.cli import LarkCLIError
from vikingbot.extensions.feishu_helpdesk.decision import message_text
from vikingbot.extensions.feishu_helpdesk.followup import HelpdeskFollowup
from vikingbot.extensions.feishu_helpdesk.state import timestamp

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


def _ticket_data(ticket):
    result = {
        key: getattr(ticket, key)
        for key in (
            "ticket_id",
            "helpdesk_id",
            "status",
            "created_at",
            "updated_at",
            "channel",
        )
        if getattr(ticket, key, None) is not None
    }
    for field in ("guest", "agents", "collaborators"):
        value = getattr(ticket, field, None)
        if value is None:
            continue

        def user(u):
            identifier = getattr(u, "id", "")
            return {"id": getattr(identifier, "open_id", identifier) or ""}

        result[field] = [user(u) for u in value] if isinstance(value, list) else user(value)
    return result


class FeishuHelpdeskChannel(FeishuChannel):
    def __init__(
        self,
        config,
        bus,
        *,
        bot_config=None,
        provider=None,
        model=None,
        session_busy=None,
        **kwargs,
    ):
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
        self._followup = None
        self._followup_deps = {"provider": provider, "model": model, "session_busy": session_busy}

    def _event_handler_builder(self):
        return (
            super()
            ._event_handler_builder()
            .register_p2_helpdesk_ticket_created_v1(self._on_ticket_sync)
            .register_p2_helpdesk_ticket_updated_v1(self._on_ticket_sync)
            .register_p2_helpdesk_ticket_message_created_v1(self._on_ticket_message_sync)
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
            activated_at = time.time()
            if self._bot_config is not None:
                agents = getattr(self._bot_config, "agents", None)
                self._followup = HelpdeskFollowup(
                    self.config,
                    self.bus,
                    self._helpdesk_api,
                    self._bot_config.bot_data_path,
                    budget=getattr(agents, "session_context_token_budget", 3000),
                    on_sync=self._restore_ticket,
                    activated_at=activated_at,
                    **self._followup_deps,
                )
            self.bot_open_id = await self._helpdesk_api.bot_open_id()
            await self._helpdesk_api.subscribe()
            if self._followup is not None:
                await self._followup.start()
            self._helpdesk_api.start()
            self._helpdesk_ready = True
            logger.info("Feishu Helpdesk ready")
        except Exception as error:
            if self._followup is not None:
                await self._followup.close()
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
        if self._followup is not None:
            self._followup.running = False
        tasks = self._helpdesk_tasks | set(self._join_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._followup is not None:
            await self._followup.close()
        self._helpdesk_tasks.clear()
        self._join_tasks.clear()
        self._ticket_chats.clear()
        await self._helpdesk_api.close()

    def _on_ticket_sync(self, data):
        # The SDK dispatches from its WebSocket thread; all state belongs to the bot loop.
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._schedule_ticket, data)

    def _on_ticket_message_sync(self, data):
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._schedule_ticket_message, data)

    def _schedule_ticket_message(self, data):
        if self._running and self._helpdesk_ready and self._followup is not None:
            task = asyncio.create_task(self._on_ticket_message(data))
            self._helpdesk_tasks.add(task)
            task.add_done_callback(partial(self._ticket_done, data=data))

    async def _on_ticket_message(self, data):
        if data.header.app_id and data.header.app_id != self.config.app_id:
            return
        service = self._followup
        event = data.event
        ticket_id = str(getattr(getattr(event, "ticket", None), "ticket_id", "") or "")
        if service is None or not service.running or not ticket_id:
            return
        item = service.store.tickets.get(ticket_id)
        if item is None:
            item = service.register(await self._helpdesk_api.ticket(ticket_id))
        if item is None or item["status"] in CLOSED_STATUSES:
            return
        chat = getattr(event, "chat_id", "") or ""
        if chat.startswith("oc_") and item["chat_id"] and item["chat_id"] != chat:
            raise ValueError("Conflicting ticket chat identity")
        if chat.startswith("oc_") and (not item["chat_id"] or item["chat_id"] == chat):
            service.store.save(item, chat_id=chat)
        service.observe(
            ticket_id,
            {
                "im_id": event.message_id or "",
                "ticket_id": event.ticket_message_id or "",
                "sender": getattr(event.sender_id, "open_id", "") or "",
                "human": (
                    False
                    if event.msg_type in ("system", "system_message")
                    else True
                    if event.sender_type in (2, 3)
                    else False
                    if event.sender_type == 1
                    else None
                ),
                "time": timestamp(data.header.create_time) or service.clock(),
                "text": message_text(event.text or getattr(event, "content", "")),
                "kind": event.msg_type or "unknown",
            },
        )
        service.dirty(ticket_id)

    async def _restore_ticket(self, item):
        """Recovery uses the same admission ownership and retries as live events."""
        if not self._running:
            return
        chat_id = item["chat_id"]
        if not chat_id:
            return
        if item["status"] in CLOSED_STATUSES:
            pending = self._join_tasks.pop(chat_id, None)
            if pending is not None:
                pending.cancel()
            self._ticket_chats.pop(chat_id, None)
        elif self._ticket_chats.get(chat_id) != item["id"]:
            if chat_id not in self._join_tasks:
                # Membership retries may wait for a human to join. They must
                # not hold up recovery of other tickets or a session worker.
                task = asyncio.create_task(self._admit_chat(chat_id, item["id"]))
                self._helpdesk_tasks.add(task)
                task.add_done_callback(self._ticket_done)

    async def _on_message(self, data):
        if self._followup is not None and self._followup.running:
            if self._followup.native(data) != "pass":
                return
        await super()._on_message(data)

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
        if self._followup is not None and self._followup.running:
            item = self._followup.register(_ticket_data(ticket), chat_id=ticket.chat_id or "")
            if item is not None:
                self._followup.dirty(item["id"])
                if item["status"] in CLOSED_STATUSES:
                    await self._restore_ticket(item)
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
        await self._admit_chat(chat_id, ticket_id)

    async def _admit_chat(self, chat_id, ticket_id):
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
        ticket_id = self._ticket_chats.get(metadata.get("reply_to"))
        if not ticket_id and self._followup is not None and self._followup.running:
            item = self._followup.for_chat(metadata.get("reply_to"))
            ticket_id = item["id"] if item else None
        if ticket_id:
            metadata["helpdesk_ticket_id"] = ticket_id
            metadata["helpdesk_id"] = self.config.helpdesk.helpdesk_id
        return await super()._handle_message(*args, metadata=metadata, **kwargs)
