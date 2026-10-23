"""Helpdesk control-plane events must preserve native IM features and isolation."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from lark_oapi.api.helpdesk.v1 import P2HelpdeskTicketCreatedV1, P2HelpdeskTicketUpdatedV1
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from pydantic import ValidationError
from vikingbot.bus.events import OutboundMessage
from vikingbot.bus.queue import MessageBus
from vikingbot.channels.feishu import FeishuChannel
from vikingbot.channels.manager import ChannelManager
from vikingbot.config.schema import BotMode, FeishuChannelConfig
from vikingbot.extensions.feishu_helpdesk.api import HelpdeskAPI, HelpdeskAPIError
from vikingbot.extensions.feishu_helpdesk.channel import FeishuHelpdeskChannel
from vikingbot.extensions.feishu_helpdesk.cli import LarkCLIError


@pytest.fixture
async def channel(tmp_path):
    config = FeishuChannelConfig(
        app_id="cli_bot",
        app_secret="app_secret",
        helpdesk={"enabled": True, "helpdesk_id": "desk_1", "helpdesk_token": "desk_secret"},
    )
    channel = FeishuHelpdeskChannel(
        config, MessageBus(), bot_config=SimpleNamespace(bot_data_path=tmp_path)
    )
    channel.bot_open_id = "ou_bot"
    channel._helpdesk_api.ensure_in_chat = AsyncMock(return_value=True)
    yield channel
    await channel.stop()


def ticket_event(updated=False, **overrides):
    ticket = {
        "ticket_id": "ticket_1",
        "helpdesk_id": "desk_1",
        "chat_id": "oc_ticket",
        "status": 1,
        "stage": 1,
    }
    ticket.update(overrides)
    return (P2HelpdeskTicketUpdatedV1 if updated else P2HelpdeskTicketCreatedV1)(
        {
            "schema": "2.0",
            "header": {
                "app_id": "cli_bot",
                "event_type": "helpdesk.ticket.updated_v1"
                if updated
                else "helpdesk.ticket.created_v1",
            },
            "event": {"object": ticket, "old_object": {"stage": 1}} if updated else ticket,
        }
    )


def im_event(*, message_type="text", mentioned=True):
    contents = {
        "text": {"text": "@_user_1 帮我排查"},
        "image": {"image_key": "img_user"},
        "post": {
            "content": [
                [{"tag": "text", "text": "问题截图"}, {"tag": "img", "image_key": "img_user"}]
            ]
        },
        "interactive": {"elements": [{"tag": "markdown", "content": "用户卡片"}]},
        "file": {"file_key": "file_user", "file_name": "report.txt"},
    }
    return P2ImMessageReceiveV1(
        {
            "schema": "2.0",
            "event": {
                "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_user",
                    "chat_id": "oc_ticket",
                    "chat_type": "group",
                    "root_id": "om_topic",
                    "message_type": message_type,
                    "content": json.dumps(contents[message_type]),
                    "mentions": [{"key": "@_user_1", "id": {"open_id": "ou_bot"}, "name": "Bot"}]
                    if mentioned
                    else [],
                },
            },
        }
    )


@pytest.mark.asyncio
async def test_creation_without_chat_then_transfer_to_human_joins_bound_chat(channel):
    await channel._on_ticket(ticket_event(chat_id=""))
    channel._helpdesk_api.ensure_in_chat.assert_not_awaited()
    await channel._on_ticket(ticket_event(updated=True, stage=2, status=2))
    channel._helpdesk_api.ensure_in_chat.assert_awaited_once_with("oc_ticket")
    assert channel._ticket_chats["oc_ticket"] == "ticket_1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"helpdesk_id": "other_desk"},
        {"chat_id": ""},
        {"chat_id": "om_not_a_chat"},
        {"ticket_id": ""},
        {"status": 50},
        {"status": 51},
        {"status": 52},
        {"status": 999},
    ],
)
async def test_unrelated_malformed_or_closed_ticket_never_invites(channel, overrides):
    await channel._on_ticket(ticket_event(**overrides))
    channel._helpdesk_api.ensure_in_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrong_application_event_is_ignored(channel):
    event = ticket_event()
    event.header.app_id = "cli_other"
    await channel._on_ticket(event)
    channel._helpdesk_api.ensure_in_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_lifecycle_events_share_one_admission_attempt(channel):
    started, finish = asyncio.Event(), asyncio.Event()

    async def ensure(chat_id):
        started.set()
        await finish.wait()

    channel._helpdesk_api.ensure_in_chat.side_effect = ensure
    first = asyncio.create_task(channel._on_ticket(ticket_event()))
    await started.wait()
    second = asyncio.create_task(channel._on_ticket(ticket_event(updated=True, stage=2)))
    await asyncio.sleep(0)
    finish.set()
    await asyncio.gather(first, second)
    channel._helpdesk_api.ensure_in_chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_admission_does_not_claim_success_and_later_event_can_retry(channel):
    channel._helpdesk_api.ensure_in_chat.side_effect = RuntimeError("not invited")
    with pytest.raises(RuntimeError):
        await channel._on_ticket(ticket_event())
    assert not channel._ticket_chats
    channel._helpdesk_api.ensure_in_chat.side_effect = None
    await channel._on_ticket(ticket_event(updated=True))
    assert channel._ticket_chats["oc_ticket"] == "ticket_1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout("temporary failure"),
        httpx.ConnectError("temporary failure"),
        HelpdeskAPIError("verify membership", "invalid_json", 503),
        HelpdeskAPIError("verify membership", 99991400, 400),
        HelpdeskAPIError("verify membership", "rate_limited", 429),
        LarkCLIError("timed out", retryable=True),
        httpx.HTTPStatusError(
            "service unavailable",
            request=httpx.Request("POST", "https://example.test/token"),
            response=httpx.Response(503),
        ),
    ],
)
async def test_scheduled_ticket_recovers_without_another_event(channel, monkeypatch, failure):
    backoff = AsyncMock()
    monkeypatch.setattr("vikingbot.extensions.feishu_helpdesk.channel.asyncio.sleep", backoff)
    channel._helpdesk_api.ensure_in_chat.side_effect = [failure, True]
    channel._running = True
    channel._schedule_ticket(ticket_event())
    await asyncio.wait_for(asyncio.gather(*tuple(channel._helpdesk_tasks)), timeout=1)
    assert channel._helpdesk_api.ensure_in_chat.await_count == 2
    backoff.assert_awaited_once_with(2)
    assert channel._ticket_chats["oc_ticket"] == "ticket_1"
    assert not channel._join_tasks


@pytest.mark.asyncio
async def test_admission_retries_are_bounded_and_do_not_claim_membership(channel, monkeypatch):
    backoff = AsyncMock()
    monkeypatch.setattr("vikingbot.extensions.feishu_helpdesk.channel.asyncio.sleep", backoff)
    channel._helpdesk_api.ensure_in_chat.side_effect = httpx.ReadTimeout("temporary failure")
    with pytest.raises(httpx.ReadTimeout):
        await channel._on_ticket(ticket_event())
    assert channel._helpdesk_api.ensure_in_chat.await_count == 4
    assert [call.args[0] for call in backoff.await_args_list] == [2, 5, 10]
    assert not channel._ticket_chats
    assert not channel._join_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        HelpdeskAPIError("verify membership", 99991679, 403),
        HelpdeskAPIError("invite bot", "pending_approval_id_list"),
        LarkCLIError("command failed", code=232011),
        LarkCLIError("command failed", code=20037, authentication=True, retryable=True),
    ],
)
async def test_admission_does_not_retry_permission_or_authorization_errors(
    channel, monkeypatch, failure
):
    backoff = AsyncMock()
    monkeypatch.setattr("vikingbot.extensions.feishu_helpdesk.channel.asyncio.sleep", backoff)
    channel._helpdesk_api.ensure_in_chat.side_effect = failure
    with pytest.raises(type(failure)):
        await channel._on_ticket(ticket_event())
    channel._helpdesk_api.ensure_in_chat.assert_awaited_once()
    backoff.assert_not_awaited()
    assert not channel._ticket_chats


@pytest.mark.asyncio
@pytest.mark.parametrize("close_ticket", [True, False])
async def test_closure_and_shutdown_cancel_pending_retry(channel, monkeypatch, close_ticket):
    waiting = asyncio.Event()

    async def backoff(delay):
        waiting.set()
        await asyncio.Future()

    monkeypatch.setattr("vikingbot.extensions.feishu_helpdesk.channel.asyncio.sleep", backoff)
    channel._helpdesk_api.ensure_in_chat.side_effect = httpx.ReadTimeout("temporary failure")
    pending = asyncio.create_task(channel._on_ticket(ticket_event()))
    await asyncio.wait_for(waiting.wait(), timeout=1)
    if close_ticket:
        await channel._on_ticket(ticket_event(updated=True, status=51))
    else:
        await channel.stop()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pending, timeout=1)
    channel._helpdesk_api.ensure_in_chat.assert_awaited_once()
    assert not channel._join_tasks
    assert not channel._ticket_chats


@pytest.mark.asyncio
async def test_concurrent_events_share_retries_and_record_success(channel, monkeypatch):
    waiting, resume = asyncio.Event(), asyncio.Event()
    real_sleep = asyncio.sleep

    async def backoff(delay):
        waiting.set()
        await resume.wait()

    monkeypatch.setattr("vikingbot.extensions.feishu_helpdesk.channel.asyncio.sleep", backoff)
    channel._helpdesk_api.ensure_in_chat.side_effect = [
        httpx.ReadTimeout("temporary failure"),
        True,
    ]
    first = asyncio.create_task(channel._on_ticket(ticket_event()))
    await asyncio.wait_for(waiting.wait(), timeout=1)
    shared_task = channel._join_tasks["oc_ticket"]
    second = asyncio.create_task(channel._on_ticket(ticket_event(updated=True)))
    await real_sleep(0)
    assert channel._join_tasks["oc_ticket"] is shared_task
    resume.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
    assert channel._helpdesk_api.ensure_in_chat.await_count == 2
    assert channel._ticket_chats["oc_ticket"] == "ticket_1"


@pytest.mark.asyncio
async def test_retry_checks_membership_before_repeating_timed_out_invitation(channel, monkeypatch):
    monkeypatch.setattr("vikingbot.extensions.feishu_helpdesk.channel.asyncio.sleep", AsyncMock())
    api = channel._helpdesk_api
    api.ensure_in_chat = lambda chat_id: HelpdeskAPI.ensure_in_chat(api, chat_id)
    # The invitation was accepted server-side, but its response timed out.
    api.is_in_chat = AsyncMock(side_effect=[False, True])
    api.cli.invite_bot = AsyncMock(side_effect=LarkCLIError("timed out", retryable=True))
    await channel._on_ticket(ticket_event())
    api.cli.invite_bot.assert_awaited_once_with("oc_ticket")
    assert api.is_in_chat.await_count == 2
    assert channel._ticket_chats["oc_ticket"] == "ticket_1"


@pytest.mark.asyncio
async def test_closed_ticket_cancels_inflight_admission(channel):
    started = asyncio.Event()

    async def ensure(chat_id):
        started.set()
        await asyncio.Future()

    channel._helpdesk_api.ensure_in_chat.side_effect = ensure
    pending = asyncio.create_task(channel._on_ticket(ticket_event()))
    await started.wait()
    await channel._on_ticket(ticket_event(updated=True, status=51))
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not channel._ticket_chats


@pytest.mark.asyncio
async def test_websocket_thread_dispatch_and_shutdown_cancel_background_work(channel):
    started = asyncio.Event()

    async def ensure(chat_id):
        started.set()
        await asyncio.Future()

    channel._helpdesk_api.ensure_in_chat.side_effect = ensure
    channel._loop = asyncio.get_running_loop()
    channel._running = True
    channel._on_ticket_sync(ticket_event())
    await asyncio.wait_for(started.wait(), timeout=1)
    await channel.stop()
    assert not channel._helpdesk_tasks
    assert not channel._join_tasks
    assert channel._helpdesk_api.http.is_closed


@pytest.mark.asyncio
async def test_native_sdk_dispatcher_delivers_created_updated_and_im(channel):
    channel._on_ticket_sync = Mock()
    channel._on_message_sync = Mock()
    handler = channel._event_handler_builder().build()
    handler.do_without_validation(
        json.dumps(
            {
                "schema": "2.0",
                "header": {"event_type": "helpdesk.ticket.updated_v1"},
                "event": {"object": {"chat_id": "oc_ticket", "ticket_id": "ticket_1"}},
            }
        ).encode()
    )
    assert channel._on_ticket_sync.call_args.args[0].event.object.chat_id == "oc_ticket"
    handler.do_without_validation(
        json.dumps(
            {
                "schema": "2.0",
                "header": {"event_type": "helpdesk.ticket.created_v1"},
                "event": {"chat_id": "oc_created"},
            }
        ).encode()
    )
    assert channel._on_ticket_sync.call_args.args[0].event.chat_id == "oc_created"
    handler.do_without_validation(
        json.dumps(
            {
                "schema": "2.0",
                "header": {"event_type": "im.message.receive_v1"},
                "event": {"message": {"message_id": "om_user"}},
            }
        ).encode()
    )
    assert channel._on_message_sync.call_args.args[0].event.message.message_id == "om_user"


@pytest.mark.asyncio
async def test_subscription_failure_prevents_starting_a_partial_connection(channel, monkeypatch):
    upstream_start = AsyncMock()
    monkeypatch.setattr(FeishuChannel, "start", upstream_start)
    channel._helpdesk_api.bot_open_id = AsyncMock(return_value="ou_bot")
    channel._helpdesk_api.subscribe = AsyncMock(side_effect=RuntimeError("subscription denied"))
    with pytest.raises(RuntimeError, match="subscription denied"):
        await channel.start()
    upstream_start.assert_not_awaited()
    assert channel._helpdesk_api.http.is_closed


@pytest.mark.asyncio
async def test_skipped_unauthorized_invitation_is_not_recorded_as_admitted(channel):
    channel._helpdesk_api.ensure_in_chat.return_value = False
    await channel._on_ticket(ticket_event())
    assert not channel._ticket_chats
    assert not channel._join_tasks


@pytest.mark.asyncio
async def test_keepalive_lifecycle_follows_channel_and_does_not_replace_im(channel, monkeypatch):
    cli = channel._helpdesk_api.cli
    cli.keepalive_once = AsyncMock()
    channel._helpdesk_api.bot_open_id = AsyncMock(return_value="ou_bot")
    channel._helpdesk_api.subscribe = AsyncMock()
    native_started = asyncio.Event()

    async def native_start():
        assert cli._keepalive_task is not None
        native_started.set()
        await asyncio.Future()

    monkeypatch.setattr(FeishuChannel, "start", lambda _: native_start())
    task = asyncio.create_task(channel.start())
    await asyncio.wait_for(native_started.wait(), timeout=1)
    await channel.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert cli._keepalive_task is None
    assert channel._helpdesk_api.http.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("message_type", ["text", "image", "post", "interactive", "file"])
async def test_helpdesk_im_keeps_media_mentions_topics_and_reply_anchor(
    channel, monkeypatch, message_type
):
    monkeypatch.setattr(
        "vikingbot.channels.feishu.load_config", lambda: SimpleNamespace(mode=BotMode.NORMAL)
    )
    channel._get_user_name = AsyncMock(return_value="Alice")
    channel._get_chat_mode = AsyncMock(return_value="thread")
    channel._add_reaction = AsyncMock()
    channel._download_and_save_image = AsyncMock(return_value="/tmp/received.png")
    await channel._on_ticket(ticket_event())

    await channel._on_message(im_event(message_type=message_type))
    incoming = channel.bus.inbound.get_nowait()
    assert incoming.need_reply is True
    assert incoming.sender_id == "ou_user"
    assert incoming.session_key.type == "feishu"
    assert incoming.session_key.chat_id == "oc_ticket#om_topic"
    assert incoming.metadata["message_id"] == "om_user"
    assert incoming.metadata["reply_to"] == "oc_ticket"
    assert incoming.metadata["helpdesk_ticket_id"] == "ticket_1"
    assert incoming.media == (["/tmp/received.png"] if message_type in ("image", "post") else [])
    assert "@_user_1" not in incoming.content
    channel._add_reaction.assert_awaited_once_with("om_user", "MeMeMe")

    message_client = SimpleNamespace(reply=Mock(return_value=SimpleNamespace(success=lambda: True)))
    channel._client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=message_client))
    )
    channel._extract_and_upload_images = AsyncMock(
        return_value=("**处理结果**", [{"image_key": "img_result"}])
    )
    sent = await channel.send(
        OutboundMessage(
            session_key=incoming.session_key,
            content="结果",
            metadata=incoming.metadata,
        )
    )
    assert sent is True
    request = message_client.reply.call_args.args[0]
    assert request.message_id == "om_user"
    assert request.request_body.reply_in_thread is True
    card = json.loads(request.request_body.content)
    assert any(e.get("img_key") == "img_result" for e in card["elements"])
    assert request.request_body.msg_type == "interactive"


@pytest.mark.asyncio
async def test_no_mention_and_sender_allowlist_keep_upstream_behavior(channel, monkeypatch):
    monkeypatch.setattr(
        "vikingbot.channels.feishu.load_config", lambda: SimpleNamespace(mode=BotMode.NORMAL)
    )
    channel._get_user_name = AsyncMock(return_value="Alice")
    channel._get_chat_mode = AsyncMock(return_value="group")
    channel._add_reaction = AsyncMock()
    await channel._on_message(im_event(mentioned=False))
    assert channel.bus.inbound.get_nowait().need_reply is False
    channel._processed_message_ids.clear()
    channel.config.allow_from = ["ou_other"]
    await channel._on_message(im_event())
    assert channel.bus.inbound.empty()


@pytest.mark.asyncio
async def test_default_feishu_is_unchanged_and_helpdesk_is_explicit_opt_in(tmp_path):
    manager = ChannelManager(MessageBus())
    ordinary = FeishuChannelConfig(app_id="cli_normal")
    manager.add_channel_from_config(ordinary)
    assert type(manager.channels[ordinary.channel_key()]) is FeishuChannel
    with pytest.raises(ValidationError, match="helpdesk_id and helpdesk_token"):
        FeishuChannelConfig(helpdesk={"enabled": True})
    configured = FeishuChannelConfig(
        app_id="cli_desk",
        helpdesk={"enabled": True, "helpdesk_id": "desk_1", "helpdesk_token": "desk_secret"},
    )
    manager.add_channel_from_config(configured, bot_config=SimpleNamespace(bot_data_path=tmp_path))
    extension = manager.channels[configured.channel_key()]
    try:
        assert isinstance(extension, FeishuHelpdeskChannel)
        # Build a real SDK dispatcher: all three event registrations must coexist.
        extension._event_handler_builder().build()
        assert "desk_secret" not in repr(configured)
    finally:
        await extension.stop()
