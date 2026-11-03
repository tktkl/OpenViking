"""Events and polling produce data; all business actions run on the host session queue."""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from pathlib import Path

from loguru import logger

from vikingbot.bus.events import InboundMessage
from vikingbot.config.schema import SessionKey
from vikingbot.extensions.feishu_helpdesk.cli import LarkCLIError
from vikingbot.extensions.feishu_helpdesk.decision import (
    BUSINESS_WAIT,
    ConversationJudge,
    message_text,
)
from vikingbot.extensions.feishu_helpdesk.delivery import HelpdeskDelivery
from vikingbot.extensions.feishu_helpdesk.state import HelpdeskState, timestamp
from vikingbot.extensions.feishu_helpdesk.sync import HelpdeskSync

CLOSED = {50, 51, 52}


class StaleAction(Exception):
    """Expected invalidation, not a user-visible AgentLoop failure."""


class HelpdeskFollowup:
    def __init__(
        self,
        config,
        bus,
        api,
        data_path,
        *,
        provider=None,
        model=None,
        session_busy=None,
        budget=3000,
        clock=time.time,
        on_sync=None,
        activated_at=None,
    ):
        self.config, self.bus, self.api, self.clock = config, bus, api, clock
        self.interval = config.helpdesk.followup_interval_minutes * 60
        identity = hashlib.sha256(
            f"{config.app_id}:{config.helpdesk.helpdesk_id}".encode()
        ).hexdigest()
        self.store = HelpdeskState(
            Path(data_path) / "helpdesk" / identity / "state.sqlite3",
            clock() if activated_at is None else activated_at,
        )
        self.judge = ConversationJudge(provider, model, budget=budget)
        self.session_busy = session_busy or (lambda key: False)
        self.on_sync = on_sync
        self.sync = HelpdeskSync(self)
        self.running = False
        self._tasks, self._executions = set(), set()
        self._queued, self._synced, self._dirty = {}, set(), set()
        self._wakeup, self._sync_wakeup, self._idle = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        self._idle.set()
        self._callbacks = 0
        self._sending = {}
        self.delivery = HelpdeskDelivery(self)

    async def start(self, *, background=True):
        self.running = True
        if background:
            self._tasks = {
                asyncio.create_task(self._monitor(), name="helpdesk-reconcile"),
                asyncio.create_task(self._timer(), name="helpdesk-deadlines"),
                asyncio.create_task(self.delivery.run(), name="helpdesk-receipts"),
            }

    async def close(self):
        if self.store._owner is None:
            return
        self.running = False
        self._queued.clear()
        for task in self._tasks | self._executions:
            task.cancel()
        await asyncio.gather(*(self._tasks | self._executions), return_exceptions=True)
        await self._idle.wait()
        self._tasks.clear()
        self._sending.clear()
        self.store.close()

    def register(self, ticket, *, chat_id="", authoritative=False):
        if str(ticket.get("helpdesk_id") or "") != self.config.helpdesk.helpdesk_id:
            return None
        item = self.store.register(
            ticket, self.clock(), chat_id=chat_id, authoritative=authoritative
        )
        if item is not None:
            self._wakeup.set()
        return item

    def dirty(self, ticket_id):
        self._dirty.add(ticket_id)
        self._sync_wakeup.set()

    def for_chat(self, chat_id, *, include_closed=False):
        return max(
            (
                t
                for t in self.store.tickets.values()
                if t["chat_id"] == chat_id and (include_closed or t["status"] not in CLOSED)
            ),
            key=lambda t: t["created"],
            default=None,
        )

    def observe(self, ticket_id, message):
        if not self.running or ticket_id not in self.store.tickets:
            return
        if self.delivery.observe(ticket_id, message):
            message = {**message, "human": False, "automatic": True}
        changed = self.store.observe(ticket_id, message)
        if changed:
            self._wakeup.set()

    def native(self, data):
        """Observe before native parsing, permissions, media I/O or steer can await."""
        event = data.event
        message, sender = event.message, event.sender
        mid = message.message_id
        if self.store.automatic(mid):
            return "drop"
        item = self.for_chat(message.chat_id) or self.for_chat(message.chat_id, include_closed=True)
        if item is None:
            return "pass"
        sender_id = getattr(getattr(sender, "sender_id", None), "open_id", "") or ""
        human = {"user": True, "app": False, "bot": False}.get(getattr(sender, "sender_type", ""))
        kind = getattr(message, "message_type", "unknown")
        if kind in ("system", "system_message"):
            human = False
        record = {
            "im_id": mid,
            "sender": sender_id,
            "human": human,
            "kind": kind,
            "time": timestamp(getattr(message, "create_time", None)) or self.clock(),
            "text": message_text(getattr(message, "content", "")),
        }
        if self.delivery.observe(item["id"], record):
            self.observe(item["id"], {**record, "human": False, "automatic": True})
            return "drop"
        if item["status"] not in CLOSED:
            self.observe(item["id"], record)
        return "pass"

    def _key(self, item):
        # Ordinary Helpdesk chats use the native group key. Topic turns remain
        # distinct; the ticket-wide busy callback protects them before closure.
        return SessionKey(
            type="feishu", channel_id=self.config.channel_id(), chat_id=item["chat_id"]
        )

    def _check(self, ticket_id, generation, *, closed=False):
        item = self.store.tickets[ticket_id]
        if not self.running or item["generation"] != generation or item["paused"]:
            raise StaleAction()
        if not closed and item["status"] in CLOSED:
            raise StaleAction()
        return item

    async def reconcile(self, *, discover=True, ids=None):
        if discover:
            try:
                await self.sync.discover()
            except Exception as error:
                logger.warning("Helpdesk discovery failed: {}", type(error).__name__)
        targets = (
            [
                t["id"]
                for t in self.store.tickets.values()
                if t["status"] not in CLOSED
                or t["notice_pending"]
                or t["close_pending"]
                or t.get("refresh_pending")
            ]
            if ids is None
            else list(ids)
        )
        for ticket_id in targets:
            if not self.running:
                return
            try:
                await self.sync.ticket(ticket_id)
                self._synced.add(ticket_id)
            except Exception as error:
                self._synced.discard(ticket_id)
                logger.warning(
                    "Helpdesk synchronization failed: ticket={}, error={}",
                    ticket_id,
                    type(error).__name__,
                )
        self._wakeup.set()

    async def _monitor(self):
        next_scan = 0
        while self.running:
            scan = self.clock() >= next_scan
            if scan:
                next_scan = self.clock() + 60
            dirty, self._dirty = self._dirty, set()
            self._sync_wakeup.clear()
            await self.reconcile(discover=scan, ids=None if scan else dirty)
            try:
                await asyncio.wait_for(
                    self._sync_wakeup.wait(), max(0.1, min(60, next_scan - self.clock()))
                )
            except asyncio.TimeoutError:
                pass

    def _due(self, item):
        if item["paused"] or self.clock() < item["retry_at"]:
            return False
        if item["status"] in CLOSED:
            return item["notice_pending"] or item["close_pending"] is not None
        if item["welcome_at"] is None or item["close_pending"] is not None:
            return True
        if item["evaluated_generation"] != item["generation"] or item["scenario"] == "resolved":
            return True
        base = item["last_send"] or item["last_human"] or item["welcome_at"]
        return self.clock() >= base + self.interval and (
            item["count"] < 2 or item["scenario"] in BUSINESS_WAIT
        )

    async def tick(self):
        for item in list(self.store.tickets.values()):
            if (
                not self.running
                or item["id"] not in self._synced
                or not item["chat_id"]
                or item["id"] in self._queued
                or not self._due(item)
            ):
                continue
            await self._enqueue(item)

    async def _timer(self):
        while self.running:
            self._wakeup.clear()
            await self.tick()
            try:
                await asyncio.wait_for(self._wakeup.wait(), self._next_wait())
            except asyncio.TimeoutError:
                pass

    def _next_wait(self):
        """Wake at the nearest durable deadline, and recalibrate wall time every 10s."""
        delays = [10.0]
        for item in self.store.tickets.values():
            if (
                item["paused"]
                or not item["chat_id"]
                or item["id"] not in self._synced
                or item["id"] in self._queued
            ):
                continue
            if item["retry_at"] > self.clock():
                delays.append(item["retry_at"] - self.clock())
            elif self._due(item):
                delays.append(0.1)
            elif item["status"] not in CLOSED and (
                item["count"] < 2 or item["scenario"] in BUSINESS_WAIT
            ):
                base = item["last_send"] or item["last_human"] or item["welcome_at"]
                if base is not None:
                    delays.append(max(0.1, base + self.interval - self.clock()))
        return max(0.1, min(delays))

    async def _enqueue(self, item):
        ticket_id, generation, token = item["id"], item["generation"], object()
        self._queued[ticket_id] = token

        def release():
            if self._queued.get(ticket_id) is token:
                del self._queued[ticket_id]

        async def guard():
            if not self.running:
                release()
                return False
            current = self.store.tickets[ticket_id]
            valid = (
                current["generation"] == generation
                and not current["paused"]
                and (
                    current["status"] not in CLOSED
                    or current["notice_pending"]
                    or current["close_pending"] is not None
                )
            )
            if not valid:
                release()
                self._wakeup.set()
            return valid

        async def execute():
            self._callbacks += 1
            self._idle.clear()
            task = asyncio.create_task(self._execute(ticket_id, generation))
            self._executions.add(task)
            try:
                (result,) = await asyncio.gather(task, return_exceptions=True)
                if isinstance(result, Exception) and not isinstance(result, StaleAction):
                    current = self.store.tickets[ticket_id]
                    if current["generation"] == generation:
                        self.store.save(current, retry_at=self.clock() + 60)
                    logger.warning(
                        "Helpdesk action failed: ticket={}, error={}",
                        ticket_id,
                        type(result).__name__,
                    )
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self._executions.discard(task)
                self._callbacks -= 1
                if not self._callbacks:
                    self._idle.set()
                release()
                self._wakeup.set()

        await self.bus.publish_inbound(
            InboundMessage(
                session_key=self._key(item),
                sender_id="helpdesk-automation",
                content="",
                need_reply=False,
                internal=True,
                before_process=guard,
                internal_handler=execute,
            )
        )

    async def _execute(self, ticket_id, generation):
        await self.sync.ticket(ticket_id)
        item = self.store.tickets[ticket_id]
        if item["status"] in CLOSED:
            await self._recover_close(item)
            item = self.store.tickets[ticket_id]
            if item["notice_pending"]:
                await self._notice(item)
            return
        item = self._check(ticket_id, generation)
        if item["close_pending"] is not None and item["close_pending"]["generation"] != generation:
            item = self.store.save(item, close_pending=None)
        for pending in self.store.pending_sends(ticket_id):
            if pending["generation"] != generation:
                if pending["kind"] == "welcome":
                    pending["generation"] = generation
                    self.store.save_outbox(f"{ticket_id}:welcome", pending)
                else:
                    self.store.save(item, paused=True, pause_reason="receipt_from_older_round")
                    logger.warning(
                        "Helpdesk automation paused: ticket={}, reason=receipt_from_older_round",
                        ticket_id,
                    )
                    raise LarkCLIError("uncertain send belongs to an older human round")
            if pending["kind"].startswith("reminder:"):
                receipt = await self._send(item, pending["kind"], pending["text"])
                current = self.store.tickets[ticket_id]
                if current["generation"] == generation:
                    self.store.save(
                        current,
                        count=int(pending["kind"].rsplit(":", 1)[1]),
                        last_send=receipt["sent_at"],
                        retry_at=0,
                    )
                return
        if item["welcome_at"] is None:
            receipt = await self._send(item, "welcome", self.config.helpdesk.messages.welcome)
            item = self.store.tickets[ticket_id]
            self.store.save(item, welcome_at=receipt["sent_at"], retry_at=0)
            return
        if item.get("reopen_waiting_for_human"):
            # Reopening is only a compatibility path. Old solved conversation
            # must not immediately close it again before any new human input.
            self.store.save(item, retry_at=self.clock() + 60)
            return
        decision = await self.judge.judge(item, self.store.messages(ticket_id))
        item = self._check(ticket_id, generation)
        item = self.store.save(
            item,
            scenario=decision.scenario,
            evaluated_generation=generation,
            retry_at=self.clock() + 60 if decision.scenario == "unknown" else 0,
        )
        if decision.scenario == "unknown":
            self.store.save(item, evaluated_generation=-1)
            return
        if decision.scenario == "resolved":
            await self._close(item, solved=2, reason=decision.reason or "沟通确认问题已解决")
            return
        base = item["last_send"] or item["last_human"] or item["welcome_at"]
        if self.clock() < base + self.interval:
            return
        if item["count"] < 2:
            text = decision.followup
            if item["count"] == 1 and decision.scenario in BUSINESS_WAIT:
                minutes = self.config.helpdesk.followup_interval_minutes
                text += "\n" + self.config.helpdesk.messages.second_reminder_suffix.format(
                    minutes=minutes
                )
            receipt = await self._send(item, f"reminder:{generation}:{item['count'] + 1}", text)
            current = self.store.tickets[ticket_id]
            if current["generation"] == generation:
                self.store.save(
                    current, count=item["count"] + 1, last_send=receipt["sent_at"], retry_at=0
                )
        elif decision.scenario in BUSINESS_WAIT:
            await self._close(item, solved=1, reason="两次提醒后业务仍未补充或确认，等待回复超时")

    async def _send(self, item, kind, text):
        key = f"{item['id']}:{kind}"
        out = self.store.outbox(key)
        if out and out.get("message_id"):
            self.delivery.settle(key, out)
            return out
        if out is None:
            out = {
                "ticket": item["id"],
                "uuid": str(uuid.uuid4()),
                "text": self.config.helpdesk.messages.render(text),
                "kind": kind,
                "generation": item["generation"],
                "attempted_at": None,
                "actor": None,
                "cycle": item.get("close_cycle", 0),
            }
            self.store.save_outbox(key, out)

        def check_expiry():
            if out["attempted_at"] is not None and self.clock() - out["attempted_at"] >= 3500:
                self.store.save(
                    self.store.tickets[item["id"]], paused=True, pause_reason="receipt_expired"
                )
                logger.warning(
                    "Helpdesk automation paused: ticket={}, reason=receipt_expired", item["id"]
                )
                raise LarkCLIError("uncertain receipt exceeds idempotency window")

        check_expiry()

        async def before(actor):
            confirmed = self.store.outbox(key)
            if confirmed and confirmed.get("message_id"):
                # Read-only recovery may finish while this caller waits for
                # the shared CLI lock. Do not overwrite or resend its receipt.
                raise StaleAction()
            self._check(item["id"], item["generation"], closed=kind.startswith("notice"))
            # Waiting for the CLI lock and authorization may exhaust the UUID
            # window. An expired attempt must go through read-only recovery.
            check_expiry()
            out.update(actor=actor, attempted_at=out["attempted_at"] or self.clock())
            self.store.save_outbox(key, out)
            self._sending[item["id"]] = actor

        try:
            receipt = await self.api.cli.send_text(
                item["chat_id"], out["text"], out["uuid"], before_call=before
            )
            sent_at = timestamp(receipt.get("create_time"))
            if sent_at is None:
                raise LarkCLIError("send returned no valid creation time", retryable=True)
            return self.delivery.confirm(
                key,
                out,
                receipt["message_id"],
                sent_at,
            )
        except (LarkCLIError, StaleAction):
            # A trusted IM echo can confirm the send before the subprocess
            # loses its response. Preserve that receipt rather than retrying.
            confirmed = self.store.outbox(key)
            if confirmed and confirmed.get("message_id"):
                return confirmed
            raise
        finally:
            self._sending.pop(item["id"], None)
            self.delivery.wakeup.set()

    async def _close(self, item, *, solved, reason):
        ticket_id, generation = item["id"], item["generation"]
        await self.sync.ticket(ticket_id)
        current = self._check(ticket_id, generation)
        if self.session_busy(self._key(current)):
            self.store.save(current, retry_at=self.clock() + 10)
            return
        if solved == 1 and (
            current["count"] != 2
            or current["scenario"] not in BUSINESS_WAIT
            or self.clock() < current["last_send"] + self.interval
        ):
            raise StaleAction()
        body = {"status": 51, "solved": solved, "comment": reason}
        pending = {"body": body, "generation": generation}
        self.store.save(current, close_pending=pending)

        async def before(_actor):
            # CLI authorization/its shared send lock can wait. Re-read after that
            # wait, immediately before the destructive request is issued.
            await self.sync.latest(ticket_id)
            latest = self._check(ticket_id, generation)
            if self.session_busy(self._key(latest)):
                self.store.save(latest, retry_at=self.clock() + 10)
                raise StaleAction()

        await self.api.cli.close_ticket(ticket_id, self.api.authorization, body, before_call=before)
        remote = await self.api.ticket(ticket_id)
        if remote.get("status") not in CLOSED:
            raise ValueError("Helpdesk close not confirmed")
        current = self.store.tickets[ticket_id]
        current = self.store.save(
            current,
            status=remote["status"],
            close_pending=None,
            closed_solved=solved,
            notice_pending=True,
            retry_at=0,
        )
        await self._notice(current)

    async def _recover_close(self, item):
        pending = item["close_pending"]
        if pending is None:
            return
        remote = await self.api.ticket(item["id"])
        comment = remote.get("comments", {}).get("content", "")
        owned = comment == pending["body"]["comment"]
        self.store.save(
            self.store.tickets[item["id"]],
            close_pending=None,
            notice_pending=owned,
            closed_solved=pending["body"]["solved"],
        )

    async def _notice(self, item):
        messages = self.config.helpdesk.messages
        text = (
            messages.closed_resolved if item.get("closed_solved") == 2 else messages.closed_timeout
        )
        cycle = item.get("close_cycle", 0)
        await self._send(item, f"notice:{cycle}" if cycle else "notice", text)
        self.store.save(self.store.tickets[item["id"]], notice_pending=False)
