"""Helpdesk subscriptions and bot admission; message delivery stays in FeishuChannel."""

import asyncio
import base64
import json
import time
from collections.abc import Awaitable, Callable
from urllib.parse import quote

import httpx

from vikingbot.extensions.feishu_helpdesk.cli import LarkCLIAdapter
from vikingbot.extensions.feishu_helpdesk.config import FeishuHelpdeskConfig

TICKET_EVENTS = (
    {"type": "helpdesk.ticket", "subtype": "ticket.created_v1"},
    {"type": "helpdesk.ticket", "subtype": "ticket.updated_v1"},
    {"type": "helpdesk.ticket_message", "subtype": "ticket_message.created_v1"},
)


class HelpdeskAPIError(RuntimeError):
    """Diagnostics deliberately exclude response bodies and credentials."""

    def __init__(self, operation: str, code: object, status: int = 0):
        super().__init__(f"Feishu Helpdesk {operation} failed (code={code}, HTTP={status})")
        self.code = code
        self.status = status
        self.retryable = status not in (401, 403) and (
            status in (408, 429) or 500 <= status < 600 or code == 99991400
        )


class HelpdeskAPI:
    def __init__(
        self,
        settings: FeishuHelpdeskConfig,
        *,
        app_id: str,
        domain: str,
        tenant_token: Callable[[], Awaitable[str]],
        cli: LarkCLIAdapter | None = None,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.settings = settings
        self.app_id = app_id
        self.domain = domain.rstrip("/")
        self.tenant_token = tenant_token
        self.http = http_client or httpx.AsyncClient(timeout=15.0)
        self.cli = cli or LarkCLIAdapter(settings.cli, app_id, domain=self.domain)
        self._read_lock = asyncio.Lock()
        self._last_read = 0.0

    @property
    def authorization(self):
        return base64.b64encode(
            f"{self.settings.helpdesk_id}:{self.settings.helpdesk_token}".encode()
        ).decode("ascii")

    async def _read(self, path, *, operation, params=None):
        # The detail endpoint permits 10/s. Share a conservative budget across
        # reconciliation and execution-time checks without holding a session worker.
        async with self._read_lock:
            await asyncio.sleep(max(0, self._last_read + 0.2 - time.monotonic()))
            self._last_read = time.monotonic()
        result = await self._bot_request(
            "GET",
            path,
            operation=operation,
            headers={"X-Lark-Helpdesk-Authorization": self.authorization},
            params=params,
        )
        data = result.get("data")
        if not isinstance(data, dict):
            raise HelpdeskAPIError(operation, "invalid_response")
        return data

    async def ticket(self, ticket_id):
        data = await self._read(
            f"/open-apis/helpdesk/v1/tickets/{quote(ticket_id, safe='')}", operation="read ticket"
        )
        ticket = data.get("ticket")
        if not isinstance(ticket, dict) or str(ticket.get("ticket_id")) != ticket_id:
            raise HelpdeskAPIError("read ticket", "invalid_response")
        return ticket

    async def chat(self, chat_id):
        if not chat_id.startswith("oc_"):
            raise HelpdeskAPIError("read chat", "invalid_chat_id")
        result = await self._bot_request(
            "GET",
            f"/open-apis/im/v1/chats/{quote(chat_id, safe='')}",
            operation="read chat",
            params={"user_id_type": "open_id"},
        )
        data = result.get("data")
        if not isinstance(data, dict):
            raise HelpdeskAPIError("read chat", "invalid_response")
        return data

    async def tickets_page(self, field, start, end, page):
        if field not in ("create", "update"):
            raise ValueError("Invalid ticket time filter")
        return await self._read(
            "/open-apis/helpdesk/v1/tickets",
            operation="list tickets",
            params={
                f"{field}_time_start": int(start * 1000),
                f"{field}_time_end": int(end * 1000),
                "page": page,
                "page_size": 200,
            },
        )

    async def messages_page(self, ticket_id, start, end, page):
        return await self._read(
            f"/open-apis/helpdesk/v1/tickets/{quote(ticket_id, safe='')}/messages",
            operation="list ticket messages",
            params={
                "time_start": int(start),
                "time_end": int(end),
                "page": page,
                "page_size": 200,
            },
        )

    def start(self):
        self.cli.start()

    async def close(self):
        await self.cli.close()
        await self.http.aclose()

    async def _request(self, method: str, url: str, *, operation: str, **kwargs) -> dict:
        response = await self.http.request(method, url, **kwargs)
        try:
            result = response.json()
        except ValueError:
            raise HelpdeskAPIError(operation, "invalid_json", response.status_code) from None
        if not isinstance(result, dict) or response.is_error or result.get("code") != 0:
            code = (
                result.get("code", "invalid_response")
                if isinstance(result, dict)
                else "invalid_response"
            )
            raise HelpdeskAPIError(operation, code, response.status_code)
        return result

    async def _bot_request(self, method, path, *, operation, **kwargs):
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {await self.tenant_token()}"
        return await self._request(
            method, self.domain + path, operation=operation, headers=headers, **kwargs
        )

    async def subscribe(self):
        authorization = base64.b64encode(
            f"{self.settings.helpdesk_id}:{self.settings.helpdesk_token}".encode()
        ).decode("ascii")
        await self._bot_request(
            "POST",
            "/open-apis/helpdesk/v1/events/subscribe",
            operation="subscribe ticket events",
            headers={"X-Lark-Helpdesk-Authorization": authorization},
            json={"events": list(TICKET_EVENTS)},
        )

    async def send_bot_text(self, chat_id, text, idempotency_key, *, before_call=None):
        """Send to an ordinary IM chat as this channel's application bot."""
        if not str(chat_id).startswith("oc_"):
            raise HelpdeskAPIError("send bot message", "invalid_chat_id")
        timeout = await before_call(self.app_id) if before_call is not None else None
        request = self._bot_request(
            "POST",
            "/open-apis/im/v1/messages",
            operation="send bot message",
            params={"receive_id_type": "chat_id"},
            json={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
                "uuid": idempotency_key,
            },
        )
        result = await asyncio.wait_for(request, timeout) if timeout is not None else await request
        data = result.get("data")
        if not isinstance(data, dict):
            raise HelpdeskAPIError("send bot message", "invalid_response")
        message_id = data.get("message_id")
        if not isinstance(message_id, str) or not message_id.startswith("om_"):
            raise HelpdeskAPIError("send bot message", "invalid_response")
        return {"message_id": message_id, "create_time": data.get("create_time")}

    async def bot_open_id(self) -> str:
        result = await self._bot_request(
            "GET", "/open-apis/bot/v3/info", operation="get bot identity"
        )
        open_id = result.get("bot", {}).get("open_id")
        if not isinstance(open_id, str) or not open_id.startswith("ou_"):
            raise HelpdeskAPIError("get bot identity", "invalid_response")
        return open_id

    async def is_in_chat(self, chat_id: str) -> bool:
        try:
            result = await self._bot_request(
                "GET",
                f"/open-apis/im/v1/chats/{quote(chat_id, safe='')}/members/is_in_chat",
                operation="verify bot membership",
            )
        except HelpdeskAPIError as error:
            # Feishu can report an absent bot as HTTP 400 instead of is_in_chat=false.
            if error.status == 400 and error.code == 232006:
                return False
            raise
        member = result.get("data", {}).get("is_in_chat")
        if not isinstance(member, bool):
            raise HelpdeskAPIError("verify bot membership", "invalid_response")
        return member

    async def leave_chat(self, chat_id: str) -> bool:
        """Remove this bot via the authorized user CLI identity."""
        if not chat_id.startswith("oc_"):
            raise HelpdeskAPIError("leave chat", "invalid_chat_id")
        return await self.cli.leave_bot(chat_id)

    async def ensure_in_chat(self, chat_id: str) -> bool:
        if await self.is_in_chat(chat_id):
            return True
        detail = await self.cli.invite_bot(chat_id)
        if detail is None:
            return False
        for field in ("invalid_id_list", "not_existed_id_list", "pending_approval_id_list"):
            if self.app_id in (detail.get(field) or []):
                raise HelpdeskAPIError("invite bot", field)
        # Group permissions may take a short time to propagate after invitation.
        for delay in (0, 1, 3):
            if delay:
                await asyncio.sleep(delay)
            if await self.is_in_chat(chat_id):
                return True
        raise HelpdeskAPIError("verify bot membership after invitation", "not_in_chat")
