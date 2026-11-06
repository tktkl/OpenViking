"""Event-independent recovery with separate ticket and message watermarks."""

import asyncio

from vikingbot.extensions.feishu_helpdesk.decision import message_text, open_id
from vikingbot.extensions.feishu_helpdesk.state import timestamp


def page_items(data, field):
    items, total = data.get(field), data.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ValueError("Incomplete Helpdesk page")
    # The Helpdesk list API returns ``null`` instead of ``[]`` when a valid
    # time window contains no tickets. Treat only that zero-total shape as an
    # empty page; a null list with a positive total is still incomplete data.
    if items is None and total == 0:
        return [], total
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        raise ValueError("Incomplete Helpdesk page")
    return items, total


class HelpdeskSync:
    def __init__(self, service):
        self.service = service
        self.api, self.store, self.clock = service.api, service.store, service.clock
        self.locks = {}

    async def discover(self):
        end = self.clock()
        changed = []
        for field in ("create", "update"):
            start = max(self.store.meta("activated_at"), self.store.meta(field + "_cursor") - 60)
            changed.extend(await self._window(field, start, end))
            self.store.set_meta(field + "_cursor", end)
        return list(dict.fromkeys(changed))

    async def _window(self, field, start, end):
        changed = []
        first = await self.api.tickets_page(field, start, end, 1)
        rows, total = page_items(first, "tickets")
        if total >= 10000:
            # Page-based Helpdesk lists cap at 10,000. Never acknowledge a truncated window.
            if int(end * 1000) - int(start * 1000) < 2:
                raise ValueError("Helpdesk window cannot be completely enumerated")
            middle = int((start + end) * 500) / 1000
            changed.extend(await self._window(field, start, middle))
            changed.extend(await self._window(field, middle, end))
            return list(dict.fromkeys(changed))
        page, count = 1, 0
        expected_total, seen = total, set()
        while True:
            for ticket in rows:
                identity = str(ticket.get("ticket_id") or "")
                if not identity or identity in seen:
                    raise ValueError("Unstable Helpdesk ticket pagination")
                seen.add(identity)
                before = self.store.tickets.get(identity)
                after = self.service.register(ticket)
                if after is not None and (
                    before is None
                    or (after.get("updated") or -1) > (before.get("updated") or -1)
                    or after["status"] != before["status"]
                    or after["generation"] != before["generation"]
                    or after["chat_id"] != before["chat_id"]
                    or bool(after.get("refresh_pending"))
                    != bool(before.get("refresh_pending"))
                ):
                    changed.append(identity)
            count += len(rows)
            if count >= total:
                if count != total:
                    raise ValueError("Inconsistent Helpdesk ticket total")
                return list(dict.fromkeys(changed))
            if not rows:
                raise ValueError("Truncated Helpdesk ticket pagination")
            page += 1
            rows, total = page_items(
                await self.api.tickets_page(field, start, end, page), "tickets"
            )
            if total != expected_total:
                raise ValueError("Helpdesk ticket count changed during pagination")

    async def ticket(self, ticket_id):
        async with self.locks.setdefault(ticket_id, asyncio.Lock()):
            ticket = await self.api.ticket(ticket_id)
            item = self.service.register(ticket, authoritative=True)
            if item is None:
                raise ValueError("Ticket does not belong to this Helpdesk")
            retry_messages = [
                m
                for m in self.store.messages(ticket_id)
                if m.get("im_id", "").startswith("om_")
                and not m.get("pending_provenance")
                and (m.get("human") is None or not item["chat_id"])
            ]
            start = max(item["created"], item["message_cursor"] - 2)
            end, page, count = self.clock(), 1, 0
            expected_total, seen = None, set()
            while True:
                rows, total = page_items(
                    await self.api.messages_page(ticket_id, start, end, page), "messages"
                )
                if expected_total is not None and total != expected_total:
                    raise ValueError("Helpdesk message count changed during pagination")
                expected_total = total
                for row in rows:
                    identity = str(row.get("id") or "")
                    if identity in seen:
                        raise ValueError("Unstable Helpdesk message pagination")
                    seen.add(identity)
                    await self._message(ticket_id, row)
                count += len(rows)
                if count >= total:
                    if count != total:
                        raise ValueError("Inconsistent Helpdesk message total")
                    break
                if not rows:
                    raise ValueError("Truncated Helpdesk message pagination")
                page += 1
            item = self.store.tickets[ticket_id]
            # A past permission failure must not strand an old message outside
            # the incremental time window forever. Retry its provenance/binding.
            for message in retry_messages[-50:]:
                await self._detail(ticket_id, message)
                self.service.observe(ticket_id, message)
            item = self.store.tickets[ticket_id]
            self.store.save(item, message_cursor=end)
            if self.service.on_sync is not None:
                await self.service.on_sync(self.store.tickets[ticket_id])
            return self.store.tickets[ticket_id]

    async def latest(self, ticket_id):
        """Last check inside the CLI lock: never wait on CLI or sync locks.

        Unknown new sources invalidate the generation now; reconciliation can
        verify them after the CLI lock is released. Do not advance cursors here.
        """
        item = self.service.register(await self.api.ticket(ticket_id), authoritative=True)
        if item is None:
            raise ValueError("Ticket does not belong to this Helpdesk")
        start = max(item["created"], item["message_cursor"] - 2)
        end, page, count, expected_total, seen = self.clock(), 1, 0, None, set()
        while True:
            rows, total = page_items(
                await self.api.messages_page(ticket_id, start, end, page), "messages"
            )
            if expected_total is not None and total != expected_total:
                raise ValueError("Helpdesk message count changed during final check")
            expected_total = total
            for row in rows:
                identity = str(row.get("id") or "")
                if identity in seen:
                    raise ValueError("Unstable Helpdesk message pagination")
                seen.add(identity)
                await self._message(ticket_id, row, verify=False)
            count += len(rows)
            if count == total:
                return
            if not rows or count > total:
                raise ValueError("Incomplete final Helpdesk check")
            page += 1

    async def _message(self, ticket_id, row, *, verify=True):
        created = timestamp(row.get("created_at"))
        if created is None or not row.get("id"):
            raise ValueError("Incomplete Helpdesk message")
        im_id = str(row.get("message_id") or "")
        msg = {
            "ticket_id": str(row["id"]),
            "im_id": im_id,
            "time": created,
            "text": message_text(row.get("content", "")),
            "sender": str(row.get("user_id") or ""),
            "human": None,
            "kind": row.get("message_type", "unknown"),
        }
        if self.store.automatic(im_id):
            msg["human"] = False
        elif im_id.startswith("om_"):
            known = next((m for m in self.store.messages(ticket_id) if m.get("im_id") == im_id), {})
            if known.get("human") is not None:
                msg["human"] = known["human"]
            if verify and (msg["human"] is None or not self.store.tickets[ticket_id]["chat_id"]):
                await self._detail(ticket_id, msg)
        else:
            # Helpdesk polling returns internal message ids, not ``om_`` ids.
            # Its user_id is still authoritative for guest/agent/system sources;
            # leaving every such message unknown blocks conversation judgement.
            item = self.store.tickets[ticket_id]
            guest = open_id(item.get("guest"))
            agents = {
                open_id(user)
                for user in (item.get("agents") or []) + (item.get("collaborators") or [])
            }
            sender = msg["sender"]
            if not sender or msg["kind"] in ("system", "system_message"):
                msg["human"] = False
            elif sender == guest or sender in agents:
                msg["human"] = True
        self.service.observe(ticket_id, msg)

    async def _detail(self, ticket_id, msg):
        try:
            detail = await self.api.cli.message(msg["im_id"])
        except Exception:
            # Helpdesk user_id can also denote a system notification. Keep an
            # unverified source unknown; a later scan retries it independently.
            return
        chat = detail.get("chat_id", "")
        item = self.store.tickets[ticket_id]
        if isinstance(chat, str) and chat.startswith("oc_"):
            if item["chat_id"] and item["chat_id"] != chat:
                raise ValueError("Conflicting ticket chat identity")
            self.store.save(item, chat_id=chat)
        sender = detail.get("sender", {})
        msg["sender"] = sender.get("id", msg.get("sender", ""))
        kind = detail.get("msg_type", msg.get("kind", "unknown"))
        if sender.get("sender_type") in ("app", "bot") or kind in ("system", "system_message"):
            msg["human"] = False
        elif sender.get("sender_type") == "user":
            msg["human"] = True
