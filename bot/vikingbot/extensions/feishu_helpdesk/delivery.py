"""Read-only receipt recovery, independent of automatic business actions and pauses."""

import asyncio

from loguru import logger

from vikingbot.extensions.feishu_helpdesk.decision import message_text
from vikingbot.extensions.feishu_helpdesk.state import timestamp

RECEIPT_PAUSES = {"receipt_expired", "receipt_from_older_round"}


class HelpdeskDelivery:
    def __init__(self, service):
        self.service = service
        self.store, self.clock = service.store, service.clock
        self.wakeup = asyncio.Event()
        self._lock = asyncio.Lock()
        self._retry = {}

    def observe(self, ticket_id, message):
        """Never defer all messages from an account used by the CLI.

        Only an exact persisted send (actor, target, text and attempt window)
        can identify an early echo. Prefix alone never grants provenance.
        """
        mid = message.get("im_id", "")
        if self.store.automatic(mid):
            return True
        if not mid.startswith("om_"):
            return False
        chat = self.store.tickets[ticket_id]["chat_id"]
        matches = []
        for key, out in self.store.outboxes():
            target = out.get("chat_id") or self.store.tickets[out["ticket"]]["chat_id"]
            if out.get("message_id") or (
                out["ticket"] != ticket_id and (not chat or target != chat)
            ):
                continue
            if chat and target and chat != target:
                continue
            attempted = out.get("attempted_at")
            if (
                attempted is not None
                and out.get("actor")
                and message.get("sender") == out["actor"]
                and message.get("text") == out["text"]
                and attempted - 2 <= message.get("time", 0) <= attempted + 3500
            ):
                matches.append((key, out))
        if len(matches) == 1:
            key, out = matches[0]
            self.confirm(key, out, mid, message["time"])
        # An echo shared by several uncertain sends is still automatic, but
        # must not arbitrarily settle one ticket's reminder count or notice.
        return bool(matches)

    def confirm(self, key, out, message_id, sent_at):
        current = self.store.outbox(key) or out
        if not current.get("message_id"):
            current.update(message_id=message_id, sent_at=sent_at)
            self.store.save_outbox(key, current)
        self.settle(key, current)
        return current

    def settle(self, key, out):
        """Idempotently finish bookkeeping even after a crash following receipt storage."""
        ticket_id = out["ticket"]
        item = self.store.tickets[ticket_id]
        if not out.get("settled"):
            changes = {}
            if out["kind"] == "welcome" and item["welcome_at"] is None:
                changes["welcome_at"] = out["sent_at"]
            elif out["kind"].startswith("reminder:") and item["generation"] == out["generation"]:
                count = int(out["kind"].rsplit(":", 1)[1])
                if count > item["count"]:
                    changes.update(count=count, last_send=out["sent_at"])
            elif out["kind"].startswith("notice") and out.get("cycle", 0) == item.get(
                "close_cycle", 0
            ):
                changes["notice_pending"] = False
            if changes:
                self.store.save(item, **changes, retry_at=0)
            self.store.reconcile_automatic(ticket_id)
            # Upgrade sources deferred by the previous implementation. New
            # manual messages enter native IM immediately and need no replay.
            for msg in self.store.messages(ticket_id):
                if msg.get("deferred_human") and not self.store.automatic(msg.get("im_id")):
                    self.store.observe(
                        ticket_id,
                        {
                            **msg,
                            "human": True,
                            "pending_provenance": False,
                            "deferred_human": False,
                        },
                    )
            self.store.save_outbox(key, {**out, "settled": True})
            self.service._wakeup.set()
        item = self.store.tickets[ticket_id]
        if (
            item["paused"]
            and item.get("pause_reason") in RECEIPT_PAUSES
            and not self.store.pending_sends(ticket_id)
        ):
            self.store.save(item, paused=False, pause_reason="", retry_at=0)
            self.service._wakeup.set()

    async def reconcile(self):
        async with self._lock:
            for key, out in self.store.outboxes():
                if not self.service.running:
                    return
                if out.get("message_id"):
                    self.settle(key, out)
                    continue
                attempted = out.get("attempted_at")
                if (
                    attempted is None
                    or not out.get("actor")
                    or out["ticket"] in self.service._sending
                    or self.clock() < self._retry.get(key, 0)
                ):
                    continue
                self._retry[key] = self.clock() + 10
                try:
                    await self._find(key, out)
                except Exception as error:
                    self._retry[key] = self.clock() + 60
                    logger.warning(
                        "Helpdesk receipt reconciliation failed: ticket={}, error={}",
                        out["ticket"],
                        type(error).__name__,
                    )

    async def _find(self, key, out):
        item = self.store.tickets[out["ticket"]]
        chat = out.get("chat_id") or item["chat_id"]
        if not chat:
            return
        token, seen = "", set()
        while self.service.running:
            page = await self.service.api.cli.chat_messages(
                chat,
                out["attempted_at"] - 2,
                min(self.clock(), out["attempted_at"] + 3500),
                token,
            )
            if not self.service.running:
                return
            rows, more = page.get("items"), page.get("has_more")
            if not isinstance(rows, list) or not isinstance(more, bool):
                raise ValueError("Incomplete IM receipt page")
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or row.get("chat_id", chat) != chat
                    or row.get("deleted")
                ):
                    continue
                sender = row.get("sender", {})
                if sender.get("sender_type") != "user" or row.get("msg_type") != "text":
                    continue
                created = timestamp(row.get("create_time"))
                if created is not None and self.observe(
                    item["id"],
                    {
                        "im_id": row.get("message_id", ""),
                        "sender": sender.get("id"),
                        "text": message_text(row.get("body", {}).get("content", "")),
                        "time": created,
                    },
                ):
                    # A known receipt for another send must not satisfy this one.
                    if self.store.outbox(key).get("message_id"):
                        return
            if not more:
                return
            token = page.get("page_token")
            if not rows or not isinstance(token, str) or not token or token in seen:
                raise ValueError("Unstable IM receipt pagination")
            seen.add(token)

    async def run(self):
        while self.service.running:
            self.wakeup.clear()
            await self.reconcile()
            try:
                await asyncio.wait_for(self.wakeup.wait(), 10)
            except asyncio.TimeoutError:
                pass
