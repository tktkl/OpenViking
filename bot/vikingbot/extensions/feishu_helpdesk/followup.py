"""Events and polling produce data; all business actions run on the host session queue."""

from __future__ import annotations

import asyncio
import hashlib
import time
import traceback
import uuid
from pathlib import Path

from loguru import logger

from vikingbot.bus.events import InboundMessage
from vikingbot.config.schema import SessionKey
from vikingbot.extensions.feishu_helpdesk.cli import LarkCLIError
from vikingbot.extensions.feishu_helpdesk.decision import (
    BUSINESS_WAIT,
    ConversationJudge,
    JudgeParseError,
    message_text,
    open_id,
)
from vikingbot.extensions.feishu_helpdesk.delivery import HelpdeskDelivery
from vikingbot.extensions.feishu_helpdesk.escalation import (
    detect_system,
    normalize_system,
    render_escalation,
)
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
        reply_sender=None,
    ):
        self.config, self.bus, self.api, self.clock = config, bus, api, clock
        self.interval = config.helpdesk.followup_interval_minutes * 60
        self.escalation_interval = config.helpdesk.escalation.interval_minutes * 60
        self.reply_sender = reply_sender
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

    def _purge_unassigned_outboxes(self):
        for key, out in self.store.outboxes():
            item = self.store.tickets.get(out.get("ticket"))
            if item and item.get("paused") and item.get("pause_reason") == "actor_not_assigned":
                self.store.delete_outbox(key)

    def _retire_attempted_closed_notices(self):
        """Closed notices are informational: never retry an attempted delivery."""
        for ticket_id, item in list(self.store.tickets.items()):
            if item.get("status") not in CLOSED or not item.get("notice_pending"):
                continue
            attempted = False
            for key, out in self.store.outboxes():
                if out.get("ticket") != ticket_id or not str(out.get("kind", "")).startswith(
                    "notice"
                ):
                    continue
                if out.get("message_id"):
                    self.delivery.settle(key, out)
                    continue
                if out.get("attempted_at") is not None:
                    attempted = True
                    self.store.save_outbox(key, {**out, "dead_letter": True})
            if attempted:
                self.store.save(item, notice_pending=False, retry_at=0)
                logger.warning(
                    "Helpdesk close notice abandoned after a single attempt: ticket={}",
                    ticket_id,
                )

    def _reclassify_numeric_messages(self):
        """Repair provenance for Helpdesk messages written before this adapter."""
        for ticket_id, item in list(self.store.tickets.items()):
            guest = open_id(item.get("guest"))
            agents = {
                open_id(user)
                for user in (item.get("agents") or []) + (item.get("collaborators") or [])
            }
            for message in self.store.messages(ticket_id):
                mid = str(message.get("im_id") or "")
                if message.get("human") is not None or not mid or mid.startswith("om_"):
                    continue
                sender = str(message.get("sender") or "")
                if not sender or message.get("kind") in ("system", "system_message"):
                    human = False
                elif sender == guest or sender in agents:
                    human = True
                else:
                    continue
                self.store.observe(ticket_id, {**message, "human": human})

    async def start(self, *, background=True):
        self.running = True
        self._purge_unassigned_outboxes()
        self._reclassify_numeric_messages()
        self._retire_attempted_closed_notices()
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

    def _escalation_configured(self):
        escalation = self.config.helpdesk.escalation
        return escalation.enabled and bool(escalation.groups)

    def _escalation_target(self, system):
        normalized = normalize_system(system)
        for configured, chat_id in self.config.helpdesk.escalation.groups.items():
            if normalize_system(configured) == normalized:
                return chat_id
        return ""

    def _detect_escalation_system(self, item):
        cached = normalize_system(item.get("escalation_system"))
        if cached:
            return cached
        systems = list(self.config.helpdesk.escalation.groups)
        system = detect_system(item, self.store.messages(item["id"]), systems)
        if system:
            self.store.save(item, escalation_system=system)
        return system

    def _escalated(self, item):
        return item.get("escalation_started_at") is not None

    def _escalation_due(self, item):
        if not self._escalation_configured() or item["status"] in CLOSED:
            return False
        if item["evaluated_generation"] != item["generation"]:
            return False
        if not self._escalated(item):
            return (
                item["count"] >= 2
                and item["last_send"] is not None
                and self.clock() >= item["last_send"] + self.interval
            )
        return (
            item["escalation_last_sent"] is not None
            and self.clock() >= item["escalation_last_sent"] + self.escalation_interval
        )

    def _escalation_wait(self, item):
        if item["status"] in CLOSED or not self._escalation_configured():
            return None
        if item["evaluated_generation"] != item["generation"]:
            return 0.1
        if self._escalated(item):
            base = item.get("escalation_last_sent") or item.get("escalation_started_at")
            return max(0.1, base + self.escalation_interval - self.clock()) if base else 0.1
        if item["count"] >= 2 and item["last_send"] is not None:
            return max(0.1, item["last_send"] + self.interval - self.clock())
        return None

    def _due(self, item):
        if item["paused"] or self.clock() < item["retry_at"]:
            return False
        if item["status"] in CLOSED:
            return item["notice_pending"] or item["close_pending"] is not None
        if item["welcome_at"] is None or item["close_pending"] is not None:
            return True
        if item["evaluated_generation"] != item["generation"] or item["scenario"] == "resolved":
            return True
        escalation_wait = self._escalation_wait(item)
        if escalation_wait is not None:
            return escalation_wait <= 0.1
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
            elif item["status"] not in CLOSED:
                escalation_wait = self._escalation_wait(item)
                if escalation_wait is not None:
                    delays.append(escalation_wait)
                elif item["count"] < 2 or item["scenario"] in BUSINESS_WAIT:
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
                        delay = 15 if isinstance(result, JudgeParseError) else 60
                        self.store.save(current, retry_at=self.clock() + delay)
                    if isinstance(result, LarkCLIError):
                        error_detail = str(result)
                    else:
                        error_detail = type(result).__name__ + "\n" + "".join(
                            traceback.format_exception(
                                type(result), result, result.__traceback__
                            )
                        )
                    logger.warning(
                        "Helpdesk action failed: ticket={}, error={}",
                        ticket_id,
                        error_detail,
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

    def _defer_close(self, ticket_id, generation, error):
        current = self.store.tickets.get(ticket_id)
        if current is None or current["generation"] != generation:
            return
        delay = 5 if getattr(error, "rejected", False) else 10
        self.store.save(current, retry_at=self.clock() + delay)
        logger.warning(
            "Helpdesk close deferred; reminder/state machine remains independent: "
            "ticket={}, retry_seconds={}, error={}",
            ticket_id,
            delay,
            type(error).__name__,
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
        elif item["close_pending"] is not None:
            # An earlier PUT may have reached Feishu even though its response
            # was lost. Synchronization/recovery confirms closure; never replay
            # that old request merely because a subsequent read is still OPEN.
            self.store.save(item, retry_at=self.clock() + 60)
            return
        for pending in self.store.pending_sends(ticket_id):
            if pending["kind"].startswith("escalation:"):
                receipt = await self._send(
                    item,
                    pending["kind"],
                    pending["text"],
                    chat_id=pending.get("chat_id") or item.get("escalation_chat_id"),
                )
                current = self.store.tickets[ticket_id]
                self._record_escalation(current, pending["kind"], receipt["sent_at"])
                return
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
        if item["scenario"] != "resolved" and self._escalation_due(item):
            sent = await self._maybe_escalate(item, generation)
            if sent:
                return
            current = self.store.tickets[ticket_id]
            if not sent and current["generation"] == generation:
                # A configured route without a detectable system is retried
                # periodically; never fall back to a noisy ticket reminder.
                self.store.save(current, retry_at=self.clock() + 300)
                return
        if (
            self._escalated(item)
            and item["evaluated_generation"] == generation
            and item["scenario"] != "resolved"
        ):
            # Repeat escalation notifications use their own clock. Resolved
            # tickets continue so a failed close can be retried.
            return
        decision = await self.judge.judge(item, self.store.messages(ticket_id))
        item = self._check(ticket_id, generation)
        item = self.store.save(
            item,
            scenario=decision.scenario,
            evaluated_generation=generation,
            retry_at=self.clock() + 30 if decision.scenario == "unknown" else 0,
        )
        if decision.scenario == "unknown":
            self.store.save(item, evaluated_generation=-1)
            return
        if decision.scenario == "resolved":
            try:
                await self._close(
                    item, solved=2, reason=decision.reason or "沟通确认问题已解决"
                )
            except StaleAction:
                raise
            except Exception as error:
                self._defer_close(ticket_id, generation, error)
            return
        if self._escalated(item):
            if self._escalation_due(item):
                await self._maybe_escalate(item, generation)
            return
        base = item["last_send"] or item["last_human"] or item["welcome_at"]
        if self.clock() < base + self.interval:
            return
        if item["count"] < 2:
            text = decision.followup
            if item["count"] == 1 and (
                decision.scenario in BUSINESS_WAIT or self._escalation_configured()
            ):
                minutes = self.config.helpdesk.followup_interval_minutes
                suffix = (
                    self.config.helpdesk.messages.escalation_reminder_suffix
                    if self._escalation_configured()
                    else self.config.helpdesk.messages.second_reminder_suffix
                )
                text += "\n" + suffix.format(minutes=minutes)
            receipt = await self._send(item, f"reminder:{generation}:{item['count'] + 1}", text)
            current = self.store.tickets[ticket_id]
            if current["generation"] == generation:
                self.store.save(
                    current, count=item["count"] + 1, last_send=receipt["sent_at"], retry_at=0
                )
        elif self._escalation_configured():
            await self._maybe_escalate(item, generation)
        elif decision.scenario in BUSINESS_WAIT:
            # Backward-compatible behavior when no escalation routes exist.
            try:
                await self._close(
                    item, solved=1, reason="两次提醒后业务仍未补充或确认，等待回复超时"
                )
            except StaleAction:
                raise
            except Exception as error:
                self._defer_close(ticket_id, generation, error)

    def _record_escalation(self, item, kind, sent_at):
        if item is None or item["status"] in CLOSED:
            return item
        sequence = int(kind.rsplit(":", 1)[1])
        changes = {
            "escalation_started_at": item.get("escalation_started_at") or sent_at,
            "escalation_last_sent": max(item.get("escalation_last_sent") or 0, sent_at),
            "escalation_count": max(int(item.get("escalation_count") or 0), sequence),
            "retry_at": 0,
        }
        current = self.store.save(item, **changes)
        self._wakeup.set()
        return current

    async def _maybe_escalate(self, item, generation):
        system = self._detect_escalation_system(item)
        if not system:
            logger.warning(
                "Helpdesk escalation deferred: ticket={}, reason=system_not_detected",
                item["id"],
            )
            return False
        target = item.get("escalation_chat_id") or self._escalation_target(system)
        if not target:
            logger.warning(
                "Helpdesk escalation deferred: ticket={}, system={}, reason=route_not_found",
                item["id"],
                system,
            )
            return False
        item = self.store.save(
            item,
            escalation_system=system,
            escalation_chat_id=target,
        )
        sequence = int(item.get("escalation_count") or 0) + 1
        kind = f"escalation:{sequence}"
        text = render_escalation(
            item["id"],
            system,
            item["chat_id"],
            self.config.helpdesk.escalation.interval_minutes,
            guest=item.get("guest"),
        )
        receipt = await self._send(item, kind, text, chat_id=target)
        current = self.store.tickets[item["id"]]
        self._record_escalation(current, kind, receipt["sent_at"])
        logger.warning(
            "Helpdesk escalated: ticket={}, system={}, sequence={}",
            item["id"],
            system,
            sequence,
        )
        return True

    async def _send(self, item, kind, text, *, chat_id=None):
        escalation = kind.startswith("escalation:")
        target = chat_id or item["chat_id"]
        if not target:
            raise LarkCLIError("Helpdesk send target is missing")
        can_send = getattr(self.reply_sender, "can_send", None)
        if not escalation and can_send is not None and not can_send(item):
            for key, out in self.store.outboxes():
                if out.get("ticket") == item["id"]:
                    self.store.delete_outbox(key)
            self.store.save(
                item, paused=True, pause_reason="actor_not_assigned", retry_at=0
            )
            logger.warning(
                "Helpdesk automation paused: ticket={}, reason=actor_not_assigned",
                item["id"],
            )
            raise StaleAction()
        key = f"{item['id']}:{kind}"
        out = self.store.outbox(key)
        if out and out.get("message_id"):
            self.delivery.settle(key, out)
            return out
        if out is not None and escalation and out.get("dead_letter"):
            # An escalation receipt beyond the idempotency window is abandoned
            # without pausing ticket follow-up. The next cycle gets a new UUID.
            self.store.delete_outbox(key)
            out = None
        if out is None:
            out = {
                "ticket": item["id"],
                "chat_id": target,
                "uuid": str(uuid.uuid4()),
                "text": self.config.helpdesk.messages.render(text),
                "kind": kind,
                "generation": item["generation"],
                "attempted_at": None,
                "actor": None,
                "cycle": item.get("close_cycle", 0),
                "transport": (
                    "feishu_bot"
                    if escalation and self.config.helpdesk.escalation.sender == "bot"
                    else "lark_cli"
                    if escalation
                    else getattr(self.reply_sender, "transport", "lark_cli")
                ),
            }
            self.store.save_outbox(key, out)

        uncertain_before = out["attempted_at"] is not None

        def check_expiry():
            if out["attempted_at"] is not None and self.clock() - out["attempted_at"] >= 3500:
                if escalation:
                    self.store.save_outbox(key, {**out, "dead_letter": True})
                    logger.warning(
                        "Helpdesk escalation receipt expired: ticket={}, sequence={}",
                        item["id"],
                        kind,
                    )
                else:
                    self.store.save(
                        self.store.tickets[item["id"]],
                        paused=True,
                        pause_reason="receipt_expired",
                    )
                    logger.warning(
                        "Helpdesk automation paused: ticket={}, reason=receipt_expired",
                        item["id"],
                    )
                raise LarkCLIError("uncertain receipt exceeds idempotency window")

        check_expiry()

        async def before(actor):
            confirmed = self.store.outbox(key)
            if confirmed and confirmed.get("message_id"):
                # Read-only recovery may finish while this caller waits for
                # the shared CLI lock. Do not overwrite or resend its receipt.
                raise StaleAction()
            notice = kind.startswith("notice")
            if escalation:
                current = self.store.tickets[item["id"]]
                if (
                    current["status"] in CLOSED
                    or current.get("escalation_chat_id") != target
                ):
                    raise StaleAction()
            else:
                current = self._check(item["id"], item["generation"], closed=notice)
            if notice and (
                current["status"] not in CLOSED
                or current.get("close_cycle", 0) != out.get("cycle", 0)
            ):
                raise StaleAction()
            # Waiting for the CLI lock and authorization may exhaust the UUID
            # window. An expired attempt must go through read-only recovery.
            check_expiry()
            out.update(actor=actor, attempted_at=out["attempted_at"] or self.clock())
            self.store.save_outbox(key, out)
            self._sending[item["id"]] = actor
            return out["attempted_at"] + 3500 - self.clock()

        try:
            if escalation:
                if self.config.helpdesk.escalation.sender == "bot":
                    sender = self.api.send_bot_text
                else:
                    sender = self.api.cli.send_text
            else:
                sender = (
                    self.reply_sender.send_text
                    if self.reply_sender
                    else self.api.cli.send_text
                )
            receipt = await sender(target, out["text"], out["uuid"], before_call=before)
            sent_at = timestamp(receipt.get("create_time"))
            if sent_at is None:
                raise LarkCLIError("send returned no valid creation time", retryable=True)
            return self.delivery.confirm(
                key,
                out,
                receipt["message_id"],
                sent_at,
            )
        except (LarkCLIError, StaleAction) as error:
            # A trusted IM echo can confirm the send before the subprocess
            # loses its response. Preserve that receipt rather than retrying.
            confirmed = self.store.outbox(key)
            if confirmed and confirmed.get("message_id"):
                return confirmed
            if isinstance(error, LarkCLIError) and error.rejected and not uncertain_before:
                # A first, explicitly refused attempt has nothing to reconcile.
                # A refusal on a retry cannot disprove an earlier uncertain send.
                self.store.save_outbox(key, {**out, "attempted_at": None, "actor": None})
            raise
        finally:
            self._sending.pop(item["id"], None)
            self.delivery.wakeup.set()

    async def _close(self, item, *, solved, reason):
        ticket_id, generation = item["id"], item["generation"]

        def check_timeout(current):
            if solved == 1 and (
                current["count"] != 2
                or current["scenario"] not in BUSINESS_WAIT
                or current["last_send"] is None
                or self.clock() < current["last_send"] + self.interval
            ):
                raise StaleAction()

        await self.sync.ticket(ticket_id)
        current = self._check(ticket_id, generation)
        if self.session_busy(self._key(current)):
            self.store.save(current, retry_at=self.clock() + 10)
            return
        check_timeout(current)
        body = {"status": 51, "solved": solved, "comment": reason}
        pending = {"body": body, "generation": generation}
        prepared = False

        async def before(_actor):
            nonlocal current, prepared
            # CLI authorization/its shared send lock can wait. Re-read after that
            # wait, immediately before the destructive request is issued.
            await self.sync.latest(ticket_id)
            latest = self._check(ticket_id, generation)
            check_timeout(latest)
            if self.session_busy(self._key(latest)):
                self.store.save(latest, retry_at=self.clock() + 10)
                raise StaleAction()
            current = self.store.save(latest, close_pending=pending)
            prepared = True

        try:
            await self.api.cli.close_ticket(
                ticket_id, self.api.authorization, body, before_call=before
            )
        except (LarkCLIError, asyncio.CancelledError) as error:
            # Cancellation before the guard persists nothing. After the guard
            # returns, the adapter writes commit without an intervening await;
            # retain pending conservatively if that operation is cancelled.
            rejected = isinstance(error, LarkCLIError) and error.rejected
            latest = self.store.tickets[ticket_id]
            if (
                rejected
                and prepared
                and latest["generation"] == generation
                and latest.get("close_cycle", 0) == current.get("close_cycle", 0)
                and latest["close_pending"] == pending
            ):
                self.store.save(latest, close_pending=None)
            raise
        remote = await self.api.ticket(ticket_id)
        current = self._confirm_close_state(current, remote)
        current = self.store.save(
            current,
            close_pending=None,
            closed_solved=solved,
            notice_pending=True,
            retry_at=0,
        )
        await self._notice(current)

    def _confirm_close_state(self, item, remote):
        current = self.store.tickets[item["id"]]
        cycle = item.get("close_cycle", 0)
        if (
            not self.running
            or current["generation"] != item["generation"]
            or current.get("close_cycle", 0) != cycle
            or current["close_pending"] != item["close_pending"]
        ):
            raise StaleAction()
        # Apply the detail response before accepting a close receipt. It may
        # itself establish a reopen and invalidate this pending operation.
        current = self.register(remote, authoritative=True)
        if current is None:
            raise ValueError("Ticket does not belong to this Helpdesk")
        if remote.get("status") not in CLOSED or current["status"] not in CLOSED:
            raise ValueError("Helpdesk close not confirmed")
        if (
            current.get("close_cycle", 0) != cycle
            or current["close_pending"] != item["close_pending"]
        ):
            raise StaleAction()
        return current

    async def _recover_close(self, item):
        pending = item["close_pending"]
        if pending is None:
            return
        remote = await self.api.ticket(item["id"])
        current = self._confirm_close_state(item, remote)
        comments = remote.get("comments") or []
        if isinstance(comments, dict):
            comments = [comments]
        owned = any(
            isinstance(comment, dict)
            and comment.get("content") == pending["body"]["comment"]
            for comment in comments
        )
        self.store.save(
            current,
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
        key = f"{item['id']}:notice:{cycle}" if cycle else f"{item['id']}:notice"
        try:
            await self._send(item, f"notice:{cycle}" if cycle else "notice", text)
        except Exception as error:
            # The ticket is already closed. The notice is informational only, so
            # a failed delivery must not keep the closed ticket in a retry loop.
            out = self.store.outbox(key)
            if out is not None and not out.get("message_id"):
                self.store.save_outbox(key, {**out, "dead_letter": True})
            current = self.store.tickets[item["id"]]
            if (
                current["generation"] == item["generation"]
                and current.get("close_cycle", 0) == cycle
            ):
                self.store.save(current, notice_pending=False, retry_at=0)
            detail = str(error) if isinstance(error, LarkCLIError) else type(error).__name__
            logger.warning(
                "Helpdesk close notice abandoned: ticket={}, error={}",
                item["id"],
                detail,
            )
            return
        current = self.store.tickets[item["id"]]
        if current["generation"] == item["generation"] and current.get("close_cycle", 0) == cycle:
            self.store.save(current, notice_pending=False, retry_at=0)
