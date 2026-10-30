"""Helpdesk uses tenant auth for control operations and delegates user invites to CLI."""

import base64
import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from vikingbot.extensions.feishu_helpdesk.api import HelpdeskAPI, HelpdeskAPIError
from vikingbot.extensions.feishu_helpdesk.cli import LarkCLIAdapter
from vikingbot.extensions.feishu_helpdesk.config import FeishuHelpdeskConfig


def make_api(handler):
    cli = Mock(spec=LarkCLIAdapter)
    cli.invite_bot.return_value = {}
    return HelpdeskAPI(
        FeishuHelpdeskConfig(enabled=True, helpdesk_id="desk_1", helpdesk_token="desk_secret"),
        app_id="cli_bot",
        domain="https://open.feishu.cn",
        tenant_token=AsyncMock(return_value="tenant_token"),
        cli=cli,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.asyncio
async def test_subscribes_both_lifecycle_events_with_helpdesk_and_tenant_auth():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"code": 0})

    api = make_api(handler)
    try:
        await api.subscribe()
        request = requests[0]
        assert request.url.path == "/open-apis/helpdesk/v1/events/subscribe"
        assert request.headers["Authorization"] == "Bearer tenant_token"
        assert (
            base64.b64decode(request.headers["X-Lark-Helpdesk-Authorization"])
            == b"desk_1:desk_secret"
        )
        assert json.loads(request.content) == {
            "events": [
                {"type": "helpdesk.ticket", "subtype": "ticket.created_v1"},
                {"type": "helpdesk.ticket", "subtype": "ticket.updated_v1"},
            ]
        }
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_already_joined_chat_does_not_require_cli_login():
    def handler(request):
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer tenant_token"
        return httpx.Response(200, json={"code": 0, "data": {"is_in_chat": True}})

    api = make_api(handler)
    try:
        assert await api.ensure_in_chat("oc_ticket") is True
        api.cli.invite_bot.assert_not_awaited()
    finally:
        await api.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("absent_as_error", [False, True])
async def test_invitation_delegates_to_cli_and_verifies_the_bot_identity(absent_as_error):
    requests = []
    joined = False

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert "X-Lark-Helpdesk-Authorization" not in request.headers
        assert request.headers["Authorization"] == "Bearer tenant_token"
        if not joined and absent_as_error:
            return httpx.Response(400, json={"code": 232006, "msg": "bot not in chat"})
        return httpx.Response(200, json={"code": 0, "data": {"is_in_chat": joined}})

    async def invite(chat_id):
        nonlocal joined
        joined = True
        return {}

    api = make_api(handler)
    api.cli.invite_bot.side_effect = invite
    try:
        assert await api.ensure_in_chat("oc_ticket") is True
        api.cli.invite_bot.assert_awaited_once_with("oc_ticket")
        assert len(requests) == 2
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_signed_out_cli_skips_invitation_without_false_membership():
    api = make_api(
        lambda request: httpx.Response(200, json={"code": 0, "data": {"is_in_chat": False}})
    )
    api.cli.invite_bot.return_value = None
    try:
        assert await api.ensure_in_chat("oc_ticket") is False
    finally:
        await api.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("absent_as_error", [False, True])
async def test_zero_cli_success_is_not_proof_of_membership(monkeypatch, absent_as_error):
    monkeypatch.setattr("vikingbot.extensions.feishu_helpdesk.api.asyncio.sleep", AsyncMock())
    requests = []

    def handler(request):
        requests.append(request)
        if absent_as_error:
            return httpx.Response(400, json={"code": 232006})
        return httpx.Response(200, json={"code": 0, "data": {"is_in_chat": False}})

    api = make_api(handler)
    try:
        with pytest.raises(HelpdeskAPIError, match="not_in_chat"):
            await api.ensure_in_chat("oc_ticket")
        api.cli.invite_bot.assert_awaited_once()
        assert len(requests) == 4
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_membership_http_400_232006_means_not_in_chat():
    api = make_api(lambda request: httpx.Response(400, json={"code": 232006}))
    try:
        assert await api.is_in_chat("oc_ticket") is False
        api.cli.invite_bot.assert_not_awaited()
    finally:
        await api.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code"),
    [(400, 232011), (401, 232006), (403, 232006), (500, 232006), (200, 232006)],
)
async def test_other_membership_errors_keep_their_code_and_http_status(status, code):
    api = make_api(lambda request: httpx.Response(status, json={"code": code}))
    try:
        with pytest.raises(HelpdeskAPIError) as failure:
            await api.is_in_chat("oc_ticket")
        assert failure.value.code == code
        assert failure.value.status == status
        api.cli.invite_bot.assert_not_awaited()
    finally:
        await api.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["subscribe", "bot_open_id"])
async def test_232006_is_not_suppressed_for_other_operations(operation):
    api = make_api(lambda request: httpx.Response(400, json={"code": 232006}))
    try:
        with pytest.raises(HelpdeskAPIError) as failure:
            await getattr(api, operation)()
        assert failure.value.code == 232006
        assert failure.value.status == 400
    finally:
        await api.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["invalid_id_list", "not_existed_id_list", "pending_approval_id_list"]
)
async def test_partial_or_pending_invitation_is_not_membership(field):
    api = make_api(
        lambda request: httpx.Response(200, json={"code": 0, "data": {"is_in_chat": False}})
    )
    api.cli.invite_bot.return_value = {field: ["cli_bot"]}
    try:
        with pytest.raises(HelpdeskAPIError, match=field):
            await api.ensure_in_chat("oc_ticket")
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_forbidden_is_not_retried_and_diagnostics_do_not_echo_credentials():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(403, json={"code": 232011, "msg": "user_token desk_secret"})

    api = make_api(handler)
    try:
        with pytest.raises(HelpdeskAPIError, match="232011") as failure:
            await api.ensure_in_chat("oc_ticket")
        assert len(requests) == 1
        assert "user_token" not in str(failure.value)
        assert "desk_secret" not in str(failure.value)
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_api_starts_keepalive_and_closes_it_before_http():
    api = make_api(lambda request: httpx.Response(200, json={"code": 0}))

    async def close_cli():
        assert not api.http.is_closed

    api.cli.close.side_effect = close_cli
    api.start()
    api.cli.start.assert_called_once()
    await api.close()
    assert api.http.is_closed
