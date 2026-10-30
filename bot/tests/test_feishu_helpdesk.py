"""Helpdesk control-plane events must preserve native IM features and isolation."""

import asyncio
import json
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from lark_oapi.api.helpdesk.v1 import P2HelpdeskTicketCreatedV1, P2HelpdeskTicketUpdatedV1
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from loguru import logger
from pydantic import ValidationError
from vikingbot.agent.context import ContextBuilder
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
        LarkCLIError("command failed", code=232011),
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
    channel._helpdesk_ready = True
    channel._schedule_ticket(ticket_event())
    await asyncio.wait_for(asyncio.gather(*tuple(channel._helpdesk_tasks)), timeout=1)
    assert channel._helpdesk_api.ensure_in_chat.await_count == 2
    backoff.assert_awaited_once_with(2)
    assert channel._ticket_chats["oc_ticket"] == "ticket_1"
    assert not channel._join_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [httpx.ReadTimeout("temporary failure"), LarkCLIError("command failed", code=232011)],
)
async def test_admission_retries_are_bounded_and_later_event_can_recover(
    channel, monkeypatch, failure
):
    backoff = AsyncMock()
    monkeypatch.setattr("vikingbot.extensions.feishu_helpdesk.channel.asyncio.sleep", backoff)
    channel._helpdesk_api.ensure_in_chat.side_effect = failure
    with pytest.raises(type(failure)):
        await channel._on_ticket(ticket_event())
    assert channel._helpdesk_api.ensure_in_chat.await_count == 6
    assert [call.args[0] for call in backoff.await_args_list] == [2, 5, 10, 30, 60]
    assert not channel._ticket_chats
    assert not channel._join_tasks
    channel._helpdesk_api.ensure_in_chat.side_effect = None
    await channel._on_ticket(ticket_event(updated=True))
    assert channel._helpdesk_api.ensure_in_chat.await_count == 7
    assert channel._ticket_chats["oc_ticket"] == "ticket_1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        HelpdeskAPIError("verify membership", 99991679, 403),
        HelpdeskAPIError("verify membership", 232011, 403),
        HelpdeskAPIError("invite bot", "pending_approval_id_list"),
        LarkCLIError("command failed", code=232017),
        LarkCLIError("command failed", code=232011, authentication=True),
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
@pytest.mark.parametrize(
    "failure",
    [httpx.ReadTimeout("temporary failure"), LarkCLIError("command failed", code=232011)],
)
async def test_closure_and_shutdown_cancel_pending_retry(
    channel, monkeypatch, close_ticket, failure
):
    waiting = asyncio.Event()

    async def backoff(delay):
        waiting.set()
        await asyncio.Future()

    monkeypatch.setattr("vikingbot.extensions.feishu_helpdesk.channel.asyncio.sleep", backoff)
    channel._helpdesk_api.ensure_in_chat.side_effect = failure
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
@pytest.mark.parametrize(
    "failure",
    [httpx.ReadTimeout("temporary failure"), LarkCLIError("command failed", code=232011)],
)
async def test_concurrent_events_share_retries_and_record_success(channel, monkeypatch, failure):
    waiting, resume = asyncio.Event(), asyncio.Event()
    real_sleep = asyncio.sleep

    async def backoff(delay):
        waiting.set()
        await resume.wait()

    monkeypatch.setattr("vikingbot.extensions.feishu_helpdesk.channel.asyncio.sleep", backoff)
    channel._helpdesk_api.ensure_in_chat.side_effect = [
        failure,
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
@pytest.mark.parametrize("updated", [False, True])
@pytest.mark.parametrize(
    ("failure", "code", "status"),
    [
        (LarkCLIError("access_secret", code=232011), 232011, 0),
        (HelpdeskAPIError("access_secret", 99991400, 400), 99991400, 400),
        (
            httpx.HTTPStatusError(
                "access_secret",
                request=httpx.Request("GET", "https://example.test?token=access_secret"),
                response=httpx.Response(503),
            ),
            None,
            503,
        ),
    ],
)
async def test_admission_logs_context_and_safe_error_details(
    channel, monkeypatch, updated, failure, code, status
):
    monkeypatch.setattr("vikingbot.extensions.feishu_helpdesk.channel.asyncio.sleep", AsyncMock())
    channel._helpdesk_api.ensure_in_chat.side_effect = failure
    channel._running = True
    channel._helpdesk_ready = True
    records = []
    sink = logger.add(lambda message: records.append(message.record))
    try:
        channel._schedule_ticket(ticket_event(updated=updated))
        await asyncio.wait_for(
            asyncio.gather(*tuple(channel._helpdesk_tasks), return_exceptions=True), timeout=1
        )
    finally:
        logger.remove(sink)

    warnings = [r["message"] for r in records if r["level"].name == "WARNING"]
    errors = [r["message"] for r in records if r["level"].name == "ERROR"]
    assert len(warnings) == 5
    assert len(errors) == 1
    for message in [*warnings, *errors]:
        assert "ticket_id=ticket_1" in message
        assert "chat_id=oc_ticket" in message
        assert f"error={type(failure).__name__}" in message
        assert f"code={code}" in message
        assert f"HTTP={status}" in message
        assert "secret" not in message
        assert "example.test" not in message
    for attempt, (message, delay) in enumerate(
        zip(warnings, (2, 5, 10, 30, 60), strict=True), start=2
    ):
        assert f"attempt={attempt}" in message
        assert f"delay={delay}s" in message
    assert not channel._helpdesk_tasks
    assert not channel._join_tasks
    assert not channel._ticket_chats


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
async def test_closure_after_membership_check_cannot_restore_ticket_binding(channel, monkeypatch):
    real_shield = asyncio.shield

    async def close_before_admission_resumes(task):
        result = await real_shield(task)
        # Membership succeeded, but the outer ticket handler has not resumed yet.
        await channel._on_ticket(ticket_event(updated=True, status=51))
        return result

    monkeypatch.setattr(
        "vikingbot.extensions.feishu_helpdesk.channel.asyncio.shield",
        close_before_admission_resumes,
    )
    await channel._on_ticket(ticket_event())
    assert not channel._ticket_chats
    assert not channel._join_tasks


@pytest.mark.asyncio
async def test_stop_clears_ticket_binding_before_late_im_is_received(channel):
    await channel._on_ticket(ticket_event())
    assert channel._ticket_chats["oc_ticket"] == "ticket_1"
    await channel.stop()
    await channel._handle_message(
        sender_id="ou_user",
        chat_id="oc_ticket",
        content="late IM event",
        metadata={"chat_type": "group", "reply_to": "oc_ticket"},
    )
    incoming = channel.bus.inbound.get_nowait()
    assert "helpdesk_id" not in incoming.metadata
    assert "helpdesk_ticket_id" not in incoming.metadata


@pytest.mark.asyncio
async def test_websocket_thread_dispatch_and_shutdown_cancel_background_work(channel):
    started = asyncio.Event()

    async def ensure(chat_id):
        started.set()
        await asyncio.Future()

    channel._helpdesk_api.ensure_in_chat.side_effect = ensure
    channel._loop = asyncio.get_running_loop()
    channel._running = True
    channel._helpdesk_ready = True
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
@pytest.mark.parametrize("operation", ["bot_open_id", "subscribe"])
@pytest.mark.parametrize(
    "failure", [HelpdeskAPIError("initialize", 99991679, 403), httpx.ReadTimeout("timeout")]
)
async def test_helpdesk_initialization_failure_keeps_native_im_active(
    channel, monkeypatch, operation, failure
):
    native_started, failed = asyncio.Event(), asyncio.Event()
    del channel.bot_open_id
    channel.config.bot_name = "Bot"

    async def native_start(_):
        channel._running = True
        native_started.set()
        await asyncio.Future()

    async def fail():
        failed.set()
        raise failure

    monkeypatch.setattr(FeishuChannel, "start", native_start)
    monkeypatch.setattr(
        "vikingbot.channels.feishu.load_config", lambda: SimpleNamespace(mode=BotMode.NORMAL)
    )
    channel._helpdesk_api.bot_open_id = AsyncMock(return_value="ou_bot")
    channel._helpdesk_api.subscribe = AsyncMock()
    channel._helpdesk_api.start = Mock()
    setattr(channel._helpdesk_api, operation, fail)
    task = asyncio.create_task(channel.start())
    try:
        await asyncio.wait_for(native_started.wait(), timeout=1)
        await asyncio.wait_for(failed.wait(), timeout=1)
        assert channel._running is True
        assert not task.done()
        assert channel._helpdesk_ready is False
        channel._helpdesk_api.start.assert_not_called()
        channel._schedule_ticket(ticket_event())
        channel._helpdesk_api.ensure_in_chat.assert_not_awaited()

        channel._add_reaction = AsyncMock()
        message = im_event(mentioned=False)
        message.event.message.chat_type = "p2p"
        await channel._on_message(message)
        assert channel.bus.inbound.get_nowait().need_reply is True
        channel._get_user_name = AsyncMock(return_value="Alice")
        channel._get_chat_mode = AsyncMock(return_value="group")
        group_message = im_event()
        group_message.event.message.message_id = "om_group_after_failure"
        await channel._on_message(group_message)
        incoming = channel.bus.inbound.get_nowait()
        assert incoming.need_reply is True
        assert "helpdesk_ticket_id" not in incoming.metadata
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert channel._helpdesk_api.http.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["bot_open_id", "subscribe"])
async def test_native_im_does_not_wait_for_helpdesk_and_stop_cancels_initialization(
    channel, monkeypatch, operation
):
    native_started, initializing, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    del channel.bot_open_id
    channel.config.bot_name = "Bot"

    async def native_start(_):
        channel._running = True
        native_started.set()
        await asyncio.Future()

    async def slow_initialization():
        initializing.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(FeishuChannel, "start", native_start)
    monkeypatch.setattr(
        "vikingbot.channels.feishu.load_config", lambda: SimpleNamespace(mode=BotMode.NORMAL)
    )
    channel._helpdesk_api.bot_open_id = AsyncMock(return_value="ou_bot")
    channel._helpdesk_api.subscribe = AsyncMock()
    setattr(channel._helpdesk_api, operation, slow_initialization)
    task = asyncio.create_task(channel.start())
    try:
        await asyncio.wait_for(native_started.wait(), timeout=1)
        await asyncio.wait_for(initializing.wait(), timeout=1)
        assert not task.done()
        assert channel._helpdesk_ready is False
        assert channel._helpdesk_api.cli._keepalive_task is None
        channel._schedule_ticket(ticket_event())
        channel._helpdesk_api.ensure_in_chat.assert_not_awaited()
        channel._add_reaction = AsyncMock()
        channel._get_user_name = AsyncMock(return_value="Alice")
        channel._get_chat_mode = AsyncMock(return_value="group")
        await channel._on_message(im_event())
        incoming = channel.bus.inbound.get_nowait()
        assert incoming.need_reply is True
        assert "helpdesk_ticket_id" not in incoming.metadata
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert cancelled.is_set()
    assert not channel._helpdesk_tasks
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
    keepalive_started = asyncio.Event()
    cli.keepalive_once = AsyncMock(side_effect=keepalive_started.set)
    channel._helpdesk_api.bot_open_id = AsyncMock(return_value="ou_bot")
    channel._helpdesk_api.subscribe = AsyncMock()
    native_started = asyncio.Event()

    async def native_start():
        assert cli._keepalive_task is None
        channel._running = True
        native_started.set()
        await asyncio.Future()

    monkeypatch.setattr(FeishuChannel, "start", lambda _: native_start())
    task = asyncio.create_task(channel.start())
    try:
        await asyncio.wait_for(native_started.wait(), timeout=1)
        await asyncio.wait_for(keepalive_started.wait(), timeout=1)
        assert channel._helpdesk_ready is True
        channel._schedule_ticket(ticket_event())
        await asyncio.wait_for(asyncio.gather(*tuple(channel._helpdesk_tasks)), timeout=1)
        assert channel._ticket_chats["oc_ticket"] == "ticket_1"
        await channel.stop()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert cli._keepalive_task is None
    assert channel._helpdesk_ready is False
    assert channel._helpdesk_api.http.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("message_type", ["text", "image", "post", "interactive", "file"])
async def test_helpdesk_im_keeps_media_mentions_topics_and_reply_anchor(
    channel, monkeypatch, message_type, tmp_path
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
    assert incoming.metadata["helpdesk_id"] == "desk_1"
    assert incoming.media == (["/tmp/received.png"] if message_type in ("image", "post") else [])
    assert "@_user_1" not in incoming.content
    channel._add_reaction.assert_awaited_once_with("om_user", "MeMeMe")

    prompt = await ContextBuilder(tmp_path, eval=True).build_messages(
        history=[],
        current_message=incoming.content,
        session_key=incoming.session_key,
        channel_metadata=incoming.metadata,
        ov_tools_enable=False,
    )
    source_block = next(
        m["content"]
        for m in prompt
        if m["role"] == "system"
        and m["content"].startswith(ContextBuilder.MESSAGE_SOURCE_HEADER)
    )
    assert json.loads(source_block.split("\n\n")[-1]) == {
        "source": "feishu_helpdesk",
        "channel": "feishu",
        "channel_id": incoming.session_key.channel_id,
        "session_chat_id": "oc_ticket#om_topic",
        "chat_type": "group",
        "helpdesk_id": "desk_1",
        "helpdesk_ticket_id": "ticket_1",
        "message_id": "om_user",
        "reply_to": "oc_ticket",
        "root_id": "om_topic",
    }

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
@pytest.mark.parametrize("bound_ticket", [False, True])
@pytest.mark.parametrize(
    ("allow_from", "allowed"), [([], True), (["ou_user"], True), (["ou_other"], False)]
)
async def test_sender_allowlist_applies_to_ordinary_and_ticket_chats(
    channel, monkeypatch, bound_ticket, allow_from, allowed
):
    monkeypatch.setattr(
        "vikingbot.channels.feishu.load_config", lambda: SimpleNamespace(mode=BotMode.NORMAL)
    )
    channel._get_user_name = AsyncMock(return_value="Alice")
    channel._get_chat_mode = AsyncMock(return_value="group")
    channel._add_reaction = AsyncMock()
    channel.config.allow_from = allow_from
    if bound_ticket:
        await channel._on_ticket(ticket_event())
    await channel._on_message(im_event())
    assert channel.bus.inbound.empty() == (not allowed)
    if allowed:
        incoming = channel.bus.inbound.get_nowait()
        assert incoming.need_reply is True
        assert incoming.sender_id == "ou_user"
        assert ("helpdesk_ticket_id" in incoming.metadata) is bound_ticket


@pytest.mark.asyncio
@pytest.mark.parametrize("message_type", ["text", "image", "post", "interactive", "file"])
@pytest.mark.parametrize(
    ("chat_type", "chat_mode", "mentioned", "root_id"),
    [
        ("p2p", "group", False, None),
        ("group", "group", True, None),
        ("group", "group", False, None),
        ("group", "thread", True, "om_topic"),
        ("group", "thread", True, None),
    ],
)
async def test_ordinary_im_matches_native_channel_with_helpdesk_enabled(
    channel, tmp_path, monkeypatch, message_type, chat_type, chat_mode, mentioned, root_id
):
    """Unbound IM must retain native parsing, mentions, topics, dedup and replies."""
    monkeypatch.setattr(
        "vikingbot.channels.feishu.load_config", lambda: SimpleNamespace(mode=BotMode.NORMAL)
    )
    native = FeishuChannel(
        FeishuChannelConfig(app_id="cli_bot", app_secret="app_secret"),
        MessageBus(),
        bot_config=SimpleNamespace(bot_data_path=tmp_path),
    )
    incoming_results, reply_results = [], []
    try:
        for current in (native, channel):
            current.bot_open_id = "ou_bot"
            current._get_user_name = AsyncMock(return_value="Alice")
            current._get_chat_mode = AsyncMock(return_value=chat_mode)
            current._add_reaction = AsyncMock()
            current._download_and_save_image = AsyncMock(return_value="/tmp/received.png")
            event = im_event(message_type=message_type, mentioned=mentioned)
            event.event.message.chat_type = chat_type
            event.event.message.root_id = root_id
            await current._on_message(event)
            incoming = current.bus.inbound.get_nowait()
            snapshot = asdict(incoming)
            snapshot.pop("timestamp")
            incoming_results.append(snapshot)
            assert "helpdesk_id" not in incoming.metadata
            assert "helpdesk_ticket_id" not in incoming.metadata
            assert incoming.need_reply is (chat_type == "p2p" or mentioned)
            assert current._add_reaction.await_count == int(incoming.need_reply)

            await current._on_message(event)
            assert current.bus.inbound.empty()

            message_client = SimpleNamespace(
                reply=Mock(return_value=SimpleNamespace(success=lambda: True))
            )
            current._client = SimpleNamespace(
                im=SimpleNamespace(v1=SimpleNamespace(message=message_client))
            )
            current._extract_and_upload_images = AsyncMock(return_value=("处理结果", []))
            assert await current.send(
                OutboundMessage(
                    session_key=incoming.session_key, content="结果", metadata=incoming.metadata
                )
            )
            request = message_client.reply.call_args.args[0]
            reply_results.append(
                (
                    request.message_id,
                    request.request_body.reply_in_thread,
                    request.request_body.msg_type,
                    json.loads(request.request_body.content),
                )
            )
        assert incoming_results[0] == incoming_results[1]
        assert reply_results[0] == reply_results[1]
    finally:
        await native.stop()


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
