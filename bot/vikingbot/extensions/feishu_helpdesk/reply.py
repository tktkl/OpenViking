"""Pluggable helpdesk reply transports.

The default remains the existing lark-cli user transport.  ``feishu_web`` is
kept in this isolated module so upstream channel/session code only needs the
small sender injection seam.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any

import httpx

from vikingbot.extensions.feishu_helpdesk.cli import LarkCLIError
from vikingbot.extensions.feishu_helpdesk.feed_resolver import (
    FeedResolutionError,
    FeishuWebFeedResolver,
)


def _vi(value: int) -> bytes:
    result = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            result.append(byte | 0x80)
        else:
            result.append(byte)
            return bytes(result)


def _field(number: int, value: bytes) -> bytes:
    return _vi(number << 3 | 2) + _vi(len(value)) + value


def _varint(number: int, value: int) -> bytes:
    return _vi(number << 3) + _vi(value)


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
        if shift > 63:
            break
    raise ValueError("invalid protobuf varint")


def _proto_fields(data: bytes):
    offset = 0
    while offset < len(data):
        key, offset = _read_varint(data, offset)
        number, wire = key >> 3, key & 7
        if not number:
            raise ValueError("invalid protobuf field")
        if wire == 0:
            value, offset = _read_varint(data, offset)
        elif wire == 1:
            if offset + 8 > len(data):
                raise ValueError("truncated protobuf fixed64")
            value, offset = data[offset : offset + 8], offset + 8
        elif wire == 2:
            length, offset = _read_varint(data, offset)
            if offset + length > len(data):
                raise ValueError("truncated protobuf bytes")
            value, offset = data[offset : offset + length], offset + length
        elif wire == 5:
            if offset + 4 > len(data):
                raise ValueError("truncated protobuf fixed32")
            value, offset = data[offset : offset + 4], offset + 4
        else:
            raise ValueError("unsupported protobuf wire type")
        yield number, wire, value


def _first(fields, number):
    return next((value for field, _, value in fields if field == number), None)


def decode_gateway_receipt(body: bytes) -> dict[str, Any] | None:
    """Extract the internal message id and millisecond timestamp from gateway data."""
    try:
        packet = _first(_proto_fields(body), 5)
        if not isinstance(packet, bytes):
            return None
        entity = _first(_proto_fields(packet), 1)
        if not isinstance(entity, bytes):
            return None
        fields = list(_proto_fields(entity))
        message_id = _first(fields, 1)
        if isinstance(message_id, bytes):
            message_id = message_id.decode("ascii", errors="strict")
        if not isinstance(message_id, str) or not message_id:
            return None
        created_ms = _first(fields, 62)
        if not isinstance(created_ms, int):
            seconds = _first(fields, 4)
            created_ms = seconds * 1000 if isinstance(seconds, int) else None
        if not isinstance(created_ms, int) or created_ms <= 0:
            return None
        return {"message_id": message_id, "create_time": created_ms}
    except (UnicodeDecodeError, ValueError):
        return None


def build_gateway_body(text: str, feed_id: str, client_id: str, request_id: str) -> bytes:
    """Build the Feishu Web IM gateway protobuf payload.

    The shape was derived from the browser's own send request.  Only the
    message text, target feed, and per-attempt identifiers vary.
    """
    text_bytes = text.encode()
    leaf = _field(1, text_bytes)
    part3 = _varint(1, 1) + _field(3, leaf)
    part2 = _field(1, b"3") + _field(2, part3)
    part1 = _field(1, part2)
    inner = _field(1, b"3") + _field(2, text_bytes) + _field(3, part1)
    content = _field(1, b"") + _field(14, inner)
    payload = (
        _varint(1, 4)
        + _field(2, content)
        + _field(3, feed_id.encode())
        + _field(4, b"")
        + _field(5, b"")
        + _field(6, client_id.encode())
        + _varint(7, 1)
        + _varint(9, 1)
        + _varint(12, 0)
        + _varint(15, 0)
        + _field(16, b"")
        + _varint(17, 0)
        + _varint(19, 0)
    )
    return _varint(2, 1) + _varint(3, 5) + _field(5, payload) + _field(6, request_id.encode())


def _value(source, name, default=None):
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


class FeishuWebGatewaySender:
    """Send as Thunder.Wu through the Web Messenger gateway.

    ``transport`` is persisted in the outbox so late Helpdesk echoes can be
    recognized even when the service desk rewrites the displayed sender.

    The sender obtains raw cookies from wangoon-auth-mcp and sends only the
    protobuf request.  Cached feed ids keep the hot path browser-free; a new
    Helpdesk ticket may use the isolated headless resolver once to discover
    its internal ``data-feed-id``.
    """

    transport = "feishu_web"

    def __init__(self, settings, bot_config):
        self.settings = settings
        self.bot_config = bot_config
        self._http_client = None
        self._cookie_cache = None
        self._cookie_cached_at = 0.0
        self.resolver = FeishuWebFeedResolver(
            settings, cache_path=_value(settings, "feed_cache_path", "") or None, cookie_provider=self._auth_cookies
        )

    def can_send(self, ticket):
        """Only impersonate the configured user when they are a ticket agent."""
        actor = str(_value(self.settings, "actor_open_id", "") or "")
        agents = ticket.get("agents") or []
        collaborators = ticket.get("collaborators") or []
        ids = {
            str(item.get("id") or "")
            for item in [*agents, *collaborators]
            if isinstance(item, dict)
        }
        ids.discard("")
        return not ids or actor in ids

    def bind_helpdesk(self, api, cache_path=None):
        selected = _value(self.settings, "feed_cache_path", "") or cache_path
        self.resolver.bind(
            api=api,
            cache_path=str(selected) if selected else None,
            cookie_provider=self._auth_cookies,
        )

    def _client(self):
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=30, trust_env=False)
        return self._http_client

    async def warm(self):
        try:
            cookies = await self._auth_cookies()
            await self.resolver.warm(cookies)
        except Exception:
            # Warmup must never make the channel unavailable.
            return

    async def close(self):
        await self.resolver.close()
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    def _server(self):
        servers = _value(_value(self.bot_config, "tools", None), "mcp_servers", {}) or {}
        server = servers.get(self.settings.auth_mcp_server)
        if server is None:
            raise LarkCLIError("auth MCP server is not configured", rejected=True)
        return server

    async def _auth_cookies(self, *, force=False):
        ttl = float(_value(self.settings, "cookie_cache_seconds", 60) or 0)
        if (
            not force
            and self._cookie_cache is not None
            and time.monotonic() - self._cookie_cached_at < ttl
        ):
            return list(self._cookie_cache)
        server = self._server()
        url = str(_value(server, "url", "") or "").strip()
        headers = dict(_value(server, "headers", {}) or {})
        if not url:
            raise LarkCLIError("auth MCP URL is not configured", rejected=True)
        headers["Accept"] = "application/json, text/event-stream"
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "auth_access_get",
                "arguments": {
                    "provider": self.settings.provider,
                    "environment": self.settings.environment,
                    "profile": self.settings.profile,
                    "force_refresh": False,
                },
            },
        }
        try:
            response = await self._client().post(
                url, headers=headers, json=payload, timeout=30
            )
            response.raise_for_status()
            result = response.json().get("result", {})
        except (httpx.HTTPError, ValueError) as error:
            raise LarkCLIError("auth MCP request failed", retryable=True) from error
        value = result.get("structuredContent")
        if value is None:
            content = result.get("content") or []
            try:
                value = json.loads(content[0]["text"])
            except (IndexError, KeyError, TypeError, ValueError) as error:
                raise LarkCLIError("auth MCP returned invalid data", retryable=True) from error
        if value.get("state") != "ready":
            error = value.get("error") or {}
            code = error.get("code") if isinstance(error, dict) else None
            raise LarkCLIError(f"feishu web login is {value.get('state', 'unavailable')}",
                               code=code, retryable=True)
        cookies = (value.get("access") or {}).get("cookies") or []
        if not isinstance(cookies, list) or not cookies:
            raise LarkCLIError("feishu web login returned no cookies", retryable=True)
        self._cookie_cache = list(cookies)
        self._cookie_cached_at = time.monotonic()
        return list(cookies)

    @staticmethod
    def _cookie_header(cookies):
        host = "internal-api-lark-api.feishu.cn"
        chosen = {}
        for cookie in cookies:
            if not isinstance(cookie, dict):
                continue
            domain = str(cookie.get("domain", "")).lstrip(".").lower()
            if host == domain or host.endswith("." + domain):
                chosen[str(cookie.get("name", ""))] = str(cookie.get("value", ""))
        return "; ".join(f"{name}={value}" for name, value in chosen.items() if name)

    async def _feed_id(self, chat_id: str, cookies) -> str:
        if not chat_id.startswith("oc_"):
            return chat_id
        try:
            return await self.resolver.resolve(chat_id, cookies=cookies)
        except FeedResolutionError as error:
            raise LarkCLIError(str(error), retryable=True) from error

    async def send_text(self, chat_id, text, idempotency_key, *, before_call):
        cookies = await self._auth_cookies()
        actor = self.settings.actor_open_id
        if not actor:
            raise LarkCLIError("feishu web actor is not configured", rejected=True)
        feed_id = await self._feed_id(chat_id, cookies)
        timeout = await before_call(actor)
        client_id = hashlib.sha256(idempotency_key.encode()).hexdigest()[:10]
        cookie_header = self._cookie_header(cookies)
        timeout = max(1.0, float(timeout or 30))
        auto_resolve = bool(_value(self.settings, "auto_resolve_feed_id", False))
        for attempt in range(2):
            request_id = secrets.token_hex(5)
            body = build_gateway_body(text, feed_id, client_id, request_id)
            headers = {
                "Content-Type": "application/x-protobuf",
                "X-AppID": "161471",
                "x-command": "5",
                "x-command-version": "8.3.0",
                "x-web-version": "8.3.0",
                "x-lgw-os-type": "1",
                "x-lgw-req-sdk-type": "220",
                "x-lgw-terminal-type": "2",
                "x-lsc-bizid": "1",
                "x-lsc-version": "1",
                "x-Source": "web",
                "locale": "zh-CN",
                "x-request-id": request_id,
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"),
                "Cookie": cookie_header,
            }
            try:
                response = await self._client().post(
                    self.settings.gateway_url,
                    headers=headers,
                    content=body,
                    timeout=timeout,
                )
            except httpx.HTTPError as error:
                raise LarkCLIError("feishu gateway request failed", retryable=True) from error
            if response.status_code == 200:
                receipt = decode_gateway_receipt(response.content)
                if receipt is None:
                    raise LarkCLIError("send returned no message receipt", retryable=True)
                return receipt
            if response.status_code == 401 and attempt == 0:
                cookies = await self._auth_cookies(force=True)
                cookie_header = self._cookie_header(cookies)
                continue
            if response.status_code == 400 and attempt == 0 and auto_resolve:
                # A cached mapping can survive a group/ticket recreation. A
                # rejected packet is safe to retry because it was not accepted.
                try:
                    feed_id = await self.resolver.resolve(chat_id, cookies=cookies, force=True)
                except FeedResolutionError as error:
                    raise LarkCLIError(str(error), retryable=True) from error
                continue
            raise LarkCLIError(
                "feishu gateway rejected message",
                code=response.status_code,
                retryable=response.status_code >= 500,
            )
        raise LarkCLIError("feishu gateway rejected message", retryable=True)


def build_reply_sender(settings, bot_config):
    """Return the configured reply transport, or None for the legacy default."""
    if settings is None or _value(settings, "mode", "lark_cli") != "feishu_web":
        return None
    return FeishuWebGatewaySender(settings, bot_config)


__all__ = ["build_gateway_body", "FeishuWebGatewaySender", "build_reply_sender"]
