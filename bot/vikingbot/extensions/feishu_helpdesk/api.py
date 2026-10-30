"""Helpdesk subscriptions and bot admission; message delivery stays in FeishuChannel."""

import asyncio
import base64
from collections.abc import Awaitable, Callable
from urllib.parse import quote

import httpx

from vikingbot.extensions.feishu_helpdesk.cli import LarkCLIAdapter
from vikingbot.extensions.feishu_helpdesk.config import FeishuHelpdeskConfig

TICKET_EVENTS = (
    {"type": "helpdesk.ticket", "subtype": "ticket.created_v1"},
    {"type": "helpdesk.ticket", "subtype": "ticket.updated_v1"},
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
        self.cli = cli or LarkCLIAdapter(settings.cli, app_id)

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
