"""Join Helpdesk ticket chats, then use the upstream Feishu IM implementation unchanged."""

import asyncio
import hashlib
import time
from collections import OrderedDict
from functools import partial
from pathlib import Path

import httpx
from loguru import logger

from vikingbot.channels import feishu
from vikingbot.channels.feishu import FeishuChannel
from vikingbot.extensions.feishu_helpdesk.api import HelpdeskAPI, HelpdeskAPIError
from vikingbot.extensions.feishu_helpdesk.cli import LarkCLIError
from vikingbot.extensions.feishu_helpdesk.decision import message_text
from vikingbot.extensions.feishu_helpdesk.followup import HelpdeskFollowup
from vikingbot.extensions.feishu_helpdesk.media import summarize_images
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
            "actual_agent_group",
            "hit_agent_groups",
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
        reply_sender=None,
        **kwargs,
    ):
        super().__init__(config, bus, bot_config=bot_config, **kwargs)
        self._reply_sender = reply_sender
        self._helpdesk_api = HelpdeskAPI(
            config.helpdesk,
            app_id=config.app_id,
            domain=config.domain,
            tenant_token=self._get_tenant_access_token,
        )
        if reply_sender is not None and hasattr(reply_sender, "bind_helpdesk"):
            cache_path = None
            data_path = getattr(bot_config, "bot_data_path", None)
            if data_path is not None:
                identity = hashlib.sha256(
                    f"{config.app_id}:{config.helpdesk.helpdesk_id}".encode()
                ).hexdigest()
                cache_path = Path(data_path) / "helpdesk" / identity / "feishu_feed_ids.json"
            reply_sender.bind_helpdesk(self._helpdesk_api, cache_path=cache_path)
        self._helpdesk_tasks: set[asyncio.Task] = set()
        self._resolver_task = None
        if reply_sender is not None and hasattr(reply_sender, "warm"):
            try:
                self._resolver_task = asyncio.get_running_loop().create_task(
                    reply_sender.warm(), name="feishu-feed-warmup"
                )
            except RuntimeError:
                pass
        self._helpdesk_ready = False
        self._join_tasks: dict[str, asyncio.Task] = {}
        self._media_summary_keys: set[str] = set()
        self._ticket_chats: OrderedDict[str, str] = OrderedDict()
        self._followup = None
        self._followup_deps = {
            "provider": provider,
            "model": model,
            "session_busy": session_busy,
            "reply_sender": reply_sender,
            "on_leave_chat": self._helpdesk_api.leave_chat,
        }

    def _event_handler_builder(self):
        return (
            super()
            ._event_handler_builder()
            .register_p2_helpdesk_ticket_created_v1(self._on_ticket_sync)
            .register_p2_helpdesk_ticket_updated_v1(self._on_ticket_sync)
            .register_p2_helpdesk_ticket_message_created_v1(self._on_ticket_message_sync)
            .register_p2_im_message_reaction_created_v1(self._on_reaction_sync)
            .register_p2_im_message_reaction_deleted_v1(self._on_reaction_sync)
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
            if self._resolver_task is None and self._reply_sender is not None and hasattr(
                self._reply_sender, "warm"
            ):
                self._resolver_task = asyncio.create_task(
                    self._reply_sender.warm(), name="feishu-feed-warmup"
                )
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
        self._media_summary_keys.clear()
        self._ticket_chats.clear()
        if self._resolver_task is not None:
            self._resolver_task.cancel()
            await asyncio.gather(self._resolver_task, return_exceptions=True)
            self._resolver_task = None
        if self._reply_sender is not None and hasattr(self._reply_sender, "close"):
            await self._reply_sender.close()
        await self._helpdesk_api.close()

    def _on_ticket_sync(self, data):
        # The SDK dispatches from its WebSocket thread; all state belongs to the bot loop.
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._schedule_ticket, data)

    def _on_reaction_sync(self, _data):
        # Reaction events are subscribed but carry no Helpdesk state. Registering
        # an explicit no-op prevents the SDK from logging processor-not-found.
        return

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
            # A stale create event may carry a transient chat ID. Resolve the
            # conflict against canonical ticket detail before rejecting the
            # message or binding follow-up automation to the wrong group.
            item = service.register(
                await self._helpdesk_api.ticket(ticket_id), authoritative=True
            )
            if item is None or item["status"] in CLOSED_STATUSES:
                return
            if item["chat_id"] and item["chat_id"] != chat:
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

    def _ticket_is_ignored(self, item):
        return bool(
            item
            and item.get("paused")
            and item.get("pause_reason")
            in {"actor_not_assigned", "outside_followup_scope"}
        )

    async def _restore_ticket(self, item):
        """Recovery uses the same admission ownership and retries as live events."""
        if not self._running or self._ticket_is_ignored(item):
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
                task.add_done_callback(
                    partial(
                        self._ticket_done,
                        ticket_id=item["id"],
                        chat_id=chat_id,
                    )
                )

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

    def _ticket_done(self, task, *, data=None, ticket_id=None, chat_id=None):
        self._helpdesk_tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            event = getattr(data, "event", None)
            ticket = getattr(event, "object", event)
            ticket_id = getattr(ticket, "ticket_id", None) or ticket_id
            chat_id = getattr(ticket, "chat_id", None) or chat_id
            logger.error(
                "Feishu Helpdesk event failed: ticket_id={}, chat_id={}, {}",
                ticket_id,
                chat_id,
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
        if ticket is None:
            return
        ticket_id = str(getattr(ticket, "ticket_id", "") or "")
        if not ticket_id:
            return
        if str(getattr(ticket, "helpdesk_id", "") or "") != self.config.helpdesk.helpdesk_id:
            return
        item = None
        if self._followup is not None and self._followup.running:
            try:
                item = self._followup.register(
                    _ticket_data(ticket), chat_id=ticket.chat_id or ""
                )
            except ValueError:
                # Creation events can expose a transient chat ID. Canonical
                # detail owns the durable ticket-group binding.
                item = self._followup.register(
                    await self._helpdesk_api.ticket(str(ticket.ticket_id or "")),
                    authoritative=True,
                )
            if item is not None:
                if self._ticket_is_ignored(item):
                    return
                self._followup.dirty(item["id"])
                if item["status"] in CLOSED_STATUSES:
                    await self._restore_ticket(item)
                    return
        chat_id = (item["chat_id"] if item is not None else "") or ticket.chat_id or ""
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
            content = args[2] if len(args) > 2 else kwargs.get("content", "")
            self._observe_ticket_media(
                ticket_id,
                metadata,
                kwargs.get("media_refs") or [],
                content=content,
            )
        return await super()._handle_message(*args, metadata=metadata, **kwargs)

    def _observe_ticket_media(self, ticket_id, metadata, media_refs, *, content=""):
        if not media_refs or self._followup is None or not self._followup.running:
            return
        message_id = str(metadata.get("message_id") or "")
        if not message_id:
            return
        record = {"im_id": message_id, "media_refs": media_refs}
        self._followup.observe(ticket_id, record)
        self._followup.dirty(ticket_id)

        existing = next(
            (
                message
                for message in self._followup.store.messages(ticket_id)
                if message.get("im_id") == message_id
            ),
            {},
        )
        if existing.get("image_summary"):
            return
        key = f"{ticket_id}:{message_id}"
        if key in self._media_summary_keys:
            return
        self._media_summary_keys.add(key)
        paths = [
            ref.get("cache_path")
            for ref in media_refs
            if isinstance(ref, dict) and isinstance(ref.get("cache_path"), str)
        ]
        if not paths:
            self._media_summary_keys.discard(key)
            return
        task = asyncio.create_task(
            self._summarize_ticket_media(ticket_id, message_id, paths, content=content),
            name=f"helpdesk-image-summary:{message_id}",
        )
        self._helpdesk_tasks.add(task)
        task.add_done_callback(partial(self._media_summary_done, key=key))

    def _media_summary_done(self, task, *, key):
        self._media_summary_keys.discard(key)
        self._ticket_done(task)

    async def _summarize_ticket_media(self, ticket_id, message_id, paths, *, content=""):
        if self._followup is None or not self._followup.running:
            return
        summary = await summarize_images(
            paths,
            provider=self._followup.judge.provider,
            model=self._followup.judge.model,
            context=str(content or ""),
        )
        self._followup.observe(
            ticket_id,
            {"im_id": message_id, "image_summary": summary},
        )
        self._followup.dirty(ticket_id)
