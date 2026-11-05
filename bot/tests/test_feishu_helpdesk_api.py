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
async def test_subscribes_lifecycle_and_message_events_with_helpdesk_and_tenant_auth():
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
                {"type": "helpdesk.ticket_message", "subtype": "ticket_message.created_v1"},
            ]
        }
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_bot_text_uses_official_im_api_with_tenant_identity():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"message_id": "om_escalation", "create_time": "1793785559000"},
            },
        )

    api = make_api(handler)
    actors = []
    try:
        receipt = await api.send_bot_text(
            "oc_escalation",
            "升级提醒",
            "uuid-1",
            before_call=AsyncMock(side_effect=lambda actor: actors.append(actor) or 30),
        )
        assert receipt == {
            "message_id": "om_escalation",
            "create_time": "1793785559000",
        }
        request = requests[0]
        assert request.url.path == "/open-apis/im/v1/messages"
        assert request.url.params["receive_id_type"] == "chat_id"
        assert request.headers["Authorization"] == "Bearer tenant_token"
        assert json.loads(request.content) == {
            "receive_id": "oc_escalation",
            "msg_type": "text",
            "content": json.dumps({"text": "升级提醒"}, ensure_ascii=False),
            "uuid": "uuid-1",
        }
        assert actors == ["cli_bot"]
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_leave_chat_uses_the_authorized_user_cli():
    api = make_api(lambda request: httpx.Response(500))
    api.cli.leave_bot = AsyncMock(return_value=True)
    try:
        assert await api.leave_chat("oc_ticket") is True
        api.cli.leave_bot.assert_awaited_once_with("oc_ticket")
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_cli_leave_bot_uses_user_identity_and_explicit_confirmation():
    adapter = LarkCLIAdapter(
        FeishuHelpdeskConfig().cli,
        "cli_bot",
        domain="https://open.feishu.cn",
    )
    adapter._authorization = AsyncMock(return_value={"openId": "ou_agent"})
    adapter._command = AsyncMock(return_value={"ok": True})
    assert await adapter.leave_bot("oc_ticket") is True
    args = adapter._command.await_args.args
    assert args[:4] == ("im", "chat.members", "delete", "--chat-id")
    assert "oc_ticket" in args
    assert ("--member-id-type", "app_id") == (
        args[args.index("--member-id-type")],
        args[args.index("--member-id-type") + 1],
    )
    assert json.loads(args[args.index("--data") + 1]) == {"id_list": ["cli_bot"]}
    assert "--yes" in args


@pytest.mark.asyncio
async def test_read_time_filters_use_milliseconds_for_tickets_and_seconds_for_messages(monkeypatch):
    api = make_api(lambda request: httpx.Response(500))
    api._read = AsyncMock(return_value={"tickets": [], "messages": [], "total": 0})
    try:
        await api.tickets_page("update", 1800000000.123, 1800000060.456, 3)
        args = api._read.call_args.kwargs["params"]
        assert args == {
            "update_time_start": 1800000000123,
            "update_time_end": 1800000060456,
            "page": 3,
            "page_size": 200,
        }
        await api.messages_page("ticket1", 1800000000.123, 1800000060.456, 2)
        args = api._read.call_args.kwargs["params"]
        assert args == {
            "time_start": 1800000000,
            "time_end": 1800000060,
            "page": 2,
            "page_size": 200,
        }
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_ticket_detail_must_match_requested_identity():
    api = make_api(
        lambda request: httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "ticket": {"ticket_id": "other"},
                },
            },
        )
    )
    try:
        with pytest.raises(HelpdeskAPIError):
            await api.ticket("expected")
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
