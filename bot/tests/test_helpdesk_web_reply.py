"""Web Messenger reply transport tests; no network or browser is used."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from vikingbot.extensions.feishu_helpdesk.cli import LarkCLIError
from vikingbot.extensions.feishu_helpdesk.reply import (
    FeishuWebGatewaySender,
    build_gateway_body,
    build_reply_sender,
    decode_gateway_receipt,
)

WEB_RECEIPT = bytes.fromhex(
    "100118012ad4030ad6020a133737303432303934323836383935303535343210041a133731303539343032353637363330383438303220a8b3abd7062a330a001a2f0a013312001a280a260a0133122108011a1d0a1be59b9ee689a7e68ea2e6b58befbc8ce8afb7e5bfbde795a5e3808230013801521337363531343235353332303030373636393235620a316a4d5050376541787868900d70a8b3abd70678018201013092010130980100a2011337373034323039343238363839353035353432c2011337363531343235353332303030373636393235c80101d0010182020088020b9002019802860dba02027a68c202020801c80200d00200d80200e00201f002018003009203021001a80300b2030408011000ca030c100018022000300038004000d00300d803c79983e498b9bda669e00309f003acf288ab9a34a204027a68ba0400c00400e004acf288ab9a34f80400c805860dd005c3f288ab9a3412180a133736353134323535333230303037363639323510900d22230a133736353134323535333230303037363639323518a8b3abd70630a8b3abd70638012a3a0a133736353134323535333230303037363639323512230a133736353134323535333230303037363639323518a8b3abd70630a8b3abd7063801320a37596f58376f6b635948"
)


def settings(**overrides):
    values = {
        "mode": "feishu_web",
        "auth_mcp_server": "wangoon-auth-mcp",
        "provider": "lark-qr",
        "environment": "prod",
        "profile": "default",
        "actor_open_id": "ou_actor",
        "gateway_url": "https://gateway.test/im/gateway/",
        "feed_ids": {"oc_chat": "7704147668575636511"},
        "web_url": "https://messenger.test/",
        "auto_resolve_feed_id": False,
        "browser_executable": "",
        "feed_resolution_timeout_seconds": 30,
        "feed_cache_path": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_web_sender_uses_raw_cookie_gateway_and_cached_feed_id(monkeypatch):
    captured = {}
    real_client = httpx.AsyncClient

    def handler(request):
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content
        return httpx.Response(200, content=WEB_RECEIPT)

    monkeypatch.setattr(
        "vikingbot.extensions.feishu_helpdesk.reply.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    sender = FeishuWebGatewaySender(settings(), None)
    sender._auth_cookies = AsyncMock(
        return_value=[
            {"name": "session", "value": "session-value", "domain": ".feishu.cn"},
            {"name": "other", "value": "ignored", "domain": "example.test"},
        ]
    )
    before = AsyncMock(return_value=12)
    receipt = await sender.send_text("oc_chat", "hello", "stable-key", before_call=before)

    assert captured["url"] == "https://gateway.test/im/gateway/"
    assert captured["headers"]["x-command"] == "5"
    assert "session=session-value" in captured["headers"]["cookie"]
    assert b"7704147668575636511" in captured["body"]
    assert b"hello" in captured["body"]
    assert before.await_args.args == ("ou_actor",)
    assert receipt["message_id"] == "7704209428689505542"
    assert receipt["create_time"] == 1793776040236


@pytest.mark.asyncio
async def test_web_sender_requires_a_cached_feed_id(monkeypatch):
    sender = FeishuWebGatewaySender(settings(feed_ids={}), None)
    sender._auth_cookies = AsyncMock(return_value=[{"name": "session", "value": "x", "domain": ".feishu.cn"}])
    with pytest.raises(LarkCLIError, match="feed id"):
        await sender.send_text("oc_missing", "hello", "key", before_call=AsyncMock(return_value=10))


@pytest.mark.asyncio
async def test_web_sender_refreshes_stale_cached_feed_after_400(monkeypatch):
    seen = []
    real_client = httpx.AsyncClient

    def handler(request):
        seen.append(request.content)
        if len(seen) == 1:
            return httpx.Response(400, content=b"bad packet")
        return httpx.Response(200, content=WEB_RECEIPT)

    monkeypatch.setattr(
        "vikingbot.extensions.feishu_helpdesk.reply.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    sender = FeishuWebGatewaySender(settings(auto_resolve_feed_id=True), None)
    sender._auth_cookies = AsyncMock(
        return_value=[{"name": "session", "value": "session-value", "domain": ".feishu.cn"}]
    )
    sender.resolver.resolve = AsyncMock(return_value="7704222222222222222")
    receipt = await sender.send_text(
        "oc_chat", "hello", "stable-key", before_call=AsyncMock(return_value=20)
    )
    assert len(seen) == 2
    assert b"7704222222222222222" in seen[1]
    assert sender.resolver.resolve.await_count == 2
    assert sender.resolver.resolve.await_args.kwargs["force"] is True
    assert receipt["message_id"] == "7704209428689505542"


def test_gateway_receipt_parser_uses_internal_receipt_fields():
    assert decode_gateway_receipt(WEB_RECEIPT) == {
        "message_id": "7704209428689505542",
        "create_time": 1793776040236,
    }


def test_gateway_receipt_parser_rejects_non_packet_data():
    assert decode_gateway_receipt(b"ok") is None


def test_gateway_body_builder_preserves_text_and_feed_id():
    body = build_gateway_body("检查完成", "7704147668575636511", "client-id", "request-id")
    assert "检查完成".encode() in body
    assert b"7704147668575636511" in body
    assert b"client-id" in body
    assert b"request-id" in body


def test_gateway_body_builder_has_exact_packet_wire_shape():
    body = build_gateway_body("hello", "123", "client-id", "request-id")
    assert body.hex() == (
        "100118052a4b080412220a00721e0a0133120568656c6c6f1a120a100a0133120b08011a070a0568656c6c6f"
        "1a0331323322002a003209636c69656e742d69643801480160007800820100880100980100320a726571756573742d6964"
    )


@pytest.mark.asyncio
async def test_web_sender_reuses_cached_mcp_cookies(monkeypatch):
    calls = []
    real_client = httpx.AsyncClient

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "result": {
                    "structuredContent": {
                        "state": "ready",
                        "access": {
                            "cookies": [
                                {"name": "session", "value": "x", "domain": ".feishu.cn"}
                            ]
                        },
                    }
                }
            },
        )

    monkeypatch.setattr(
        "vikingbot.extensions.feishu_helpdesk.reply.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    sender = FeishuWebGatewaySender(
        settings(),
        SimpleNamespace(
            tools=SimpleNamespace(
                mcp_servers={
                    "wangoon-auth-mcp": {"url": "https://auth.test/mcp", "headers": {}}
                }
            )
        ),
    )
    assert await sender._auth_cookies() == [
        {"name": "session", "value": "x", "domain": ".feishu.cn"}
    ]
    assert await sender._auth_cookies() == [
        {"name": "session", "value": "x", "domain": ".feishu.cn"}
    ]
    assert len(calls) == 1
    await sender.close()


def test_web_sender_only_allows_the_configured_ticket_agent():
    sender = FeishuWebGatewaySender(settings(), None)
    assert sender.can_send({"agents": [{"id": "ou_actor"}]})
    assert sender.can_send({"agents": [], "collaborators": []})
    assert not sender.can_send({"agents": [{"id": "ou_someone_else"}]})
    assert not sender.can_send({"agents": [{"id": "ou_someone_else"}], "collaborators": [{"id": "ou_other"}]})


def test_reply_sender_is_disabled_for_legacy_mode():
    assert build_reply_sender(settings(mode="lark_cli"), None) is None
    assert isinstance(build_reply_sender(settings(), None), FeishuWebGatewaySender)
