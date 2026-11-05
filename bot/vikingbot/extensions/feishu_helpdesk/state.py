"""Host-owned, durable ticket state. No OAuth or Helpdesk credentials are stored here."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import uuid
from pathlib import Path


def _identity_signature(ticket):
    """Stable ticket participants; Feishu profile/CDN fields are volatile."""

    def ids(value):
        if isinstance(value, dict):
            value = [value]
        if not isinstance(value, list):
            return ()
        return tuple(
            sorted(
                str(row.get("id") or row.get("agent_id") or row.get("user_id") or "")
                for row in value
                if isinstance(row, dict)
            )
        )

    guest = ticket.get("guest")
    return (
        str(guest.get("id") or "") if isinstance(guest, dict) else "",
        ids(ticket.get("agents")),
        ids(ticket.get("collaborators")),
    )


class HelpdeskState:
    def __init__(self, path: Path, now: float):
        import fcntl

        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._owner = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.db = sqlite3.connect(path)
            os.chmod(path, 0o600)
            self.db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tickets (id TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages
                    (id TEXT PRIMARY KEY, ticket TEXT NOT NULL, value TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS messages_ticket ON messages(ticket);
                CREATE TABLE IF NOT EXISTS aliases (id TEXT PRIMARY KEY, message TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            if self.meta("activated_at") is None:
                with self.db:
                    self.db.executemany(
                        "INSERT OR REPLACE INTO meta VALUES (?,?)",
                        [
                            (key, json.dumps(now))
                            for key in ("activated_at", "create_cursor", "update_cursor")
                        ],
                    )
            self.tickets = {
                key: json.loads(value)
                for key, value in self.db.execute("SELECT id, value FROM tickets")
            }
            for ticket_id, item in self.tickets.items():
                self._rebase_pending_outboxes(ticket_id, item.get("chat_id", ""))
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            os.close(self._owner)
            self._owner = None
            raise

    def close(self):
        if self._owner is not None:
            self.db.close()
            os.close(self._owner)
            self._owner = None

    def meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_meta(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))

    def save(self, item, **changes):
        candidate = {**item, **changes}
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO tickets VALUES (?,?)",
                (candidate["id"], json.dumps(candidate, ensure_ascii=False)),
            )
        self.tickets[candidate["id"]] = candidate
        return candidate

    def register(self, ticket, now, *, chat_id="", authoritative=False):
        ticket_id = str(ticket.get("ticket_id") or "")
        chat_id = chat_id or str(ticket.get("chat_id") or "")
        old = self.tickets.get(ticket_id)
        created = timestamp(ticket.get("created_at"))
        if not ticket_id or (
            old is None
            and (
                created is None
                or created < self.meta("activated_at")
                or ticket.get("status") not in (1, 2, 3, 4, 5)
            )
        ):
            return None
        if old is None:
            old = {
                "id": ticket_id,
                "created": created,
                "chat_id": "",
                "status": 1,
                "generation": 0,
                "count": 0,
                "last_human": None,
                "last_send": None,
                "welcome_at": None,
                "scenario": "unknown",
                "evaluated_generation": -1,
                "retry_at": 0,
                "message_cursor": created,
                "updated": -1,
                "close_pending": None,
                "notice_pending": False,
                "paused": False,
                "pause_reason": "",
                "followup_skill_group": "",
                "followup_systems": [],
                "scope_exit_pending": False,
                "scope_exit_attempted": False,
                "left_chat_at": None,
                "leave_chat_error": "",
                "escalation_system": "",
                "escalation_chat_id": "",
                "escalation_started_at": None,
                "escalation_last_sent": None,
                "escalation_count": 0,
            }
        # Trusted chat identity is independent of the event's state version.
        # A scan can learn newer status before an older event supplies the group.
        if chat_id.startswith("oc_"):
            if old["chat_id"] and old["chat_id"] != chat_id:
                if not authoritative:
                    raise ValueError("Conflicting ticket chat identity")
                # Ticket detail is the canonical source. Feishu can emit a
                # transient chat during creation and replace it with the real
                # ticket group in a later event/detail read.
                old = self.save(old, chat_id=chat_id)
                self._rebase_pending_outboxes(ticket_id, chat_id)
            elif not old["chat_id"]:
                old = self.save(old, chat_id=chat_id)
        updated = timestamp(ticket.get("updated_at"))
        if updated is not None and updated < old["updated"]:
            return old
        status = ticket.get("status", old["status"])
        if status not in (1, 2, 3, 4, 5, 50, 51, 52):
            return old
        # Late creation events frequently omit updated_at. Only a newer,
        # canonical read may reopen a ticket that we have already seen closed.
        if (
            old["status"] in (50, 51, 52)
            and status not in (50, 51, 52)
            and not (authoritative and updated is not None and updated > old["updated"])
        ):
            if updated is not None and updated > old["updated"]:
                # Persist the hint before the discovery cursor advances. Only
                # a canonical detail read may actually reopen local state.
                return self.save(old, refresh_pending=True)
            return old
        values = {"status": status, "updated": updated if updated is not None else old["updated"]}
        if authoritative:
            values["refresh_pending"] = False
        if old["status"] in (50, 51, 52) and status not in (50, 51, 52):
            values.update(
                reopened_at=now,
                reopen_waiting_for_human=True,
                # Backfilled messages from before the last closed state must
                # not unlock a reopened ticket's old resolution.
                reopen_human_after=max(old["last_human"] or old["created"], old["updated"]),
                close_cycle=old.get("close_cycle", 0) + 1,
                count=0,
                last_send=None,
                scenario="unknown",
                close_pending=None,
                notice_pending=False,
                retry_at=0,
                followup_skill_group="",
                followup_systems=[],
                scope_exit_pending=False,
                scope_exit_attempted=False,
                left_chat_at=None,
                leave_chat_error="",
                escalation_system="",
                escalation_chat_id="",
                escalation_started_at=None,
                escalation_last_sent=None,
                escalation_count=0,
            )
        old_group_state = (
            old.get("actual_agent_group"),
            old.get("hit_agent_groups"),
        )
        for key in (
            "guest",
            "agents",
            "collaborators",
            "ticket_type",
            "channel",
            "actual_agent_group",
            "hit_agent_groups",
        ):
            if key in ticket:
                values[key] = ticket[key]
        new_group_state = (
            ticket.get("actual_agent_group", old.get("actual_agent_group")),
            ticket.get("hit_agent_groups", old.get("hit_agent_groups")),
        )
        if new_group_state != old_group_state:
            # Skill-group ownership is authoritative for follow-up scope.
            # Recompute it whenever Feishu changes the current assignment.
            values.update(
                followup_skill_group="",
                followup_systems=[],
                escalation_system="",
                escalation_chat_id="",
            )
        candidate = {**old, **values}
        if status != old["status"] or _identity_signature(candidate) != _identity_signature(old):
            values["generation"] = old["generation"] + 1
            values["evaluated_generation"] = -1
        return self.save(old, **values)

    def outbox(self, key):
        row = self.db.execute("SELECT value FROM outbox WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_outbox(self, key, value):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO outbox VALUES (?,?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def delete_outbox(self, key):
        with self.db:
            self.db.execute("DELETE FROM outbox WHERE id=?", (key,))

    def _rebase_pending_outboxes(self, ticket_id, chat_id):
        """Move unconfirmed sends to the ticket's canonical chat."""
        if not chat_id:
            return
        for key, out in self.outboxes():
            if (
                out.get("ticket") == ticket_id
                and not out.get("message_id")
                and out.get("chat_id") != chat_id
            ):
                self.save_outbox(
                    key,
                    {
                        **out,
                        "chat_id": chat_id,
                        "uuid": str(uuid.uuid4()),
                        "attempted_at": None,
                        "actor": None,
                    },
                )

    def pending_sends(self, ticket_id):
        values = (json.loads(value) for (value,) in self.db.execute("SELECT value FROM outbox"))
        return [
            value
            for value in values
            if value.get("ticket") == ticket_id
            and not value.get("message_id")
            and not value.get("dead_letter")
            and value.get("attempted_at") is not None
        ]

    def outboxes(self):
        return [
            (key, json.loads(value))
            for key, value in self.db.execute("SELECT id, value FROM outbox")
        ]

    def automatic(self, message_id):
        if not message_id:
            return False
        # Receipts are few (welcome + two per human round). Keep them across restarts.
        return any(
            message_id in (value.get("message_id"), value.get("im_message_id"))
            for value in (
                json.loads(row[0])
                for row in self.db.execute("SELECT value FROM outbox")
            )
        )

    def observe(self, ticket_id, message):
        """Merge aliases before applying human activity; duplicate delivery never resets a round."""
        aliases = [
            f"{ticket_id}:{kind}:{message[kind]}"
            for kind in ("im_id", "ticket_id")
            if message.get(kind)
        ]
        if not aliases:
            return False
        keys = []
        for alias in aliases:
            row = self.db.execute("SELECT message FROM aliases WHERE id=?", (alias,)).fetchone()
            if row:
                if row[0] not in keys:
                    keys.append(row[0])
        key = keys[0] if keys else aliases[0]
        old = {}
        for existing in reversed(keys):
            row = self.db.execute("SELECT value FROM messages WHERE id=?", (existing,)).fetchone()
            if row:
                record = json.loads(row[0])
                if old.get("human") is True:
                    record["human"] = True
                old.update(record)
        merged = {**old, **{k: v for k, v in message.items() if v is not None}}
        for identity in ("im_id", "ticket_id", "sender"):
            if not merged.get(identity) and old.get(identity):
                merged[identity] = old[identity]
        if old.get("human") is not None:
            merged["human"] = old["human"]
        if self.automatic(merged.get("im_id")):
            merged["human"] = False
            merged["automatic"] = True
        newly_human = merged.get("human") is True and old.get("human") is not True
        item = self.tickets[ticket_id]
        candidate = item
        if newly_human:
            candidate = {
                **item,
                "last_human": max(merged["time"], item["last_human"] or item["created"]),
                "generation": item["generation"] + 1,
                "count": 0,
                "last_send": None,
                "scenario": "unknown",
                "retry_at": 0,
            }
            if merged["time"] > item.get("reopen_human_after", 0):
                candidate["reopen_waiting_for_human"] = False
        elif (
            merged.get("human") is not False
            and not merged.get("pending_provenance")
            and any(
                merged.get(k) != old.get(k)
                for k in ("text", "human", "sender", "kind", "image_summary")
            )
        ):
            # Unknown provenance and edited content also invalidate a decision,
            # without pretending that a system message was human activity.
            candidate = {
                **item,
                "generation": item["generation"] + 1,
                "evaluated_generation": -1,
                "retry_at": 0,
            }
        with self.db:
            for duplicate in keys[1:]:
                self.db.execute("UPDATE aliases SET message=? WHERE message=?", (key, duplicate))
                self.db.execute("DELETE FROM messages WHERE id=?", (duplicate,))
            self.db.execute(
                "INSERT OR REPLACE INTO messages VALUES (?,?,?)",
                (key, ticket_id, json.dumps(merged, ensure_ascii=False)),
            )
            for alias in aliases:
                self.db.execute("INSERT OR REPLACE INTO aliases VALUES (?,?)", (alias, key))
            if candidate is not item:
                self.db.execute(
                    "INSERT OR REPLACE INTO tickets VALUES (?,?)",
                    (ticket_id, json.dumps(candidate, ensure_ascii=False)),
                )
        self.tickets[ticket_id] = candidate
        return candidate is not item

    def messages(self, ticket_id):
        values = [
            json.loads(row[0])
            for row in self.db.execute("SELECT value FROM messages WHERE ticket=?", (ticket_id,))
        ]
        return sorted(values, key=lambda m: (m["time"], m.get("im_id") or m.get("ticket_id", "")))

    def reconcile_automatic(self, ticket_id):
        for msg in self.messages(ticket_id):
            if msg.get("human") is None and self.automatic(msg.get("im_id")):
                self.observe(ticket_id, {**msg, "human": False, "automatic": True})


def timestamp(value):
    """Feishu record timestamps are milliseconds; the local clock and deadlines use seconds."""
    try:
        result = float(value) / 1000
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError):
        return None
