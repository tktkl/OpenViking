"""Automation receipts never block native IM, including paused and closed tickets."""

import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from test_feishu_helpdesk import im_event
from test_helpdesk_followup import add_ticket, drain, human, initialized
from test_helpdesk_followup import desk as desk
from vikingbot.channels.feishu import FeishuChannel
from vikingbot.config.schema import FeishuChannelConfig
from vikingbot.extensions.feishu_helpdesk.channel import FeishuHelpdeskChannel
from vikingbot.extensions.feishu_helpdesk.cli import LarkCLIError
from vikingbot.extensions.feishu_helpdesk.decision import Decision


def native(clock, mid, text, *, sender="ou_operator"):
    event = im_event(mentioned=True)
    event.event.sender.sender_id.open_id = sender
    message = event.event.message
    message.message_id = mid
    message.create_time = str(int(clock() * 1000))
    message.content = json.dumps({"text": text})
    return event


def receipt(out, mid="om_recovered", *, chat="oc_ticket"):
    return {
        "message_id": mid,
        "chat_id": chat,
        "msg_type": "text",
        "create_time": str(int(out["attempted_at"] * 1000)),
        "sender": {"id": out["actor"], "sender_type": "user"},
        "body": {"content": json.dumps({"text": out["text"]})},
    }


async def paused(desk):
    service, api, clock = await initialized(desk)
    clock.advance(300)
    api.cli.fail_send = True
    await drain(service)
    out = service.store.pending_sends("t1")[0]
    human(service, clock, "om_new_round")
    await drain(service)
    assert service.store.tickets["t1"]["paused"]
    return service, api, clock, out


@pytest.mark.asyncio
async def test_manual_cli_user_message_reaches_agent_while_paused_and_receipt_io_blocks(
    desk,
    monkeypatch,
):
    service, api, clock, out = await paused(desk)
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked(*args):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            assert service.store._owner is not None
            cancelled.set()

    api.cli.chat_messages.side_effect = blocked
    task = asyncio.create_task(service.delivery.run())
    service._tasks.add(task)
    await asyncio.wait_for(entered.wait(), 1)
    channel = FeishuHelpdeskChannel(service.config, service.bus)
    channel._followup = service
    forwarded = AsyncMock()
    monkeypatch.setattr(FeishuChannel, "_on_message", forwarded)
    message = native(clock, "om_manual", "@Bot 请帮我排查")
    try:
        await asyncio.wait_for(channel._on_message(message), 1)
        forwarded.assert_awaited_once_with(message)
        assert service.store.messages("t1")[-1]["human"] is True
        assert service.store.tickets["t1"]["last_human"] == clock()
        assert service.store.tickets["t1"]["paused"]
    finally:
        await channel.stop()
    assert cancelled.is_set() and service.store._owner is None


@pytest.mark.asyncio
async def test_paused_receipt_recovers_without_resending_or_changing_new_round(desk):
    service, api, clock, out = await paused(desk)
    generation = service.store.tickets["t1"]["generation"]
    api.cli.chat_messages.return_value = {"items": [receipt(out)], "has_more": False}
    # Sending remains broken; the independent recovery path must only read.
    await service.delivery.reconcile()
    item = service.store.tickets["t1"]
    assert not item["paused"] and not service.store.pending_sends("t1")
    assert item["count"] == 0 and item["last_send"] is None
    assert item["generation"] == generation and len(api.cli.sends) == 1
    assert service.store.automatic("om_recovered")
    assert service.native(native(clock, "om_recovered", out["text"])) == "drop"


@pytest.mark.asyncio
async def test_receipt_recovery_survives_restart_expiry_and_template_change(desk, tmp_path):
    service, api, clock, out = await paused(desk)
    await service.close()
    clock.advance(3600)
    restarted, _, _ = await desk(
        path=tmp_path,
        clock=clock,
        api=api,
        messages={"prefix": "【新服务台标识】"},
    )
    api.cli.chat_messages.return_value = {"items": [receipt(out)], "has_more": False}
    await restarted.delivery.reconcile()
    assert not restarted.store.tickets["t1"]["paused"]
    assert restarted.store.automatic("om_recovered")
    assert len(api.cli.sends) == 1


@pytest.mark.asyncio
async def test_manual_message_is_immediate_during_send_and_survives_receipt_loss(desk):
    service, api, clock = await desk()
    add_ticket(service, api, clock)
    await service.reconcile()

    async def lost(chat, text, key, *, before_call):
        await before_call("ou_operator")
        assert service.native(native(clock, "om_manual", "我来处理")) == "pass"
        assert service.store.tickets["t1"]["last_human"] == clock()
        assert service.native(native(clock, "om_accepted", text)) == "drop"
        raise LarkCLIError("timed out", retryable=True)

    api.cli.send_text = lost
    await drain(service)
    records = {m["im_id"]: m for m in service.store.messages("t1")}
    assert records["om_manual"]["human"] is True
    assert records["om_accepted"]["human"] is False
    assert service.store.tickets["t1"]["welcome_at"] == clock()


@pytest.mark.asyncio
async def test_closed_notice_echo_is_filtered_before_and_after_receipt(desk, monkeypatch):
    service, api, clock = await initialized(desk)
    service.judge.judge.return_value = Decision("resolved", reason="已确认恢复")
    human(service, clock)
    send = api.cli.send_text
    early = []

    async def echo(chat, text, key, *, before_call):
        result = await send(chat, text, key, before_call=before_call)
        assert service.store.tickets["t1"]["status"] == 51
        early.append(service.native(native(clock, "om_auto2", text)))
        return result

    api.cli.send_text = echo
    await drain(service)
    assert early == ["drop"]
    channel = FeishuHelpdeskChannel(service.config, service.bus)
    channel._followup = service
    forwarded = AsyncMock()
    monkeypatch.setattr(FeishuChannel, "_on_message", forwarded)
    try:
        await channel._on_message(native(clock, "om_auto2", api.cli.sends[-1][1]))
        forwarded.assert_not_awaited()
        manual = native(clock, "om_closed_manual", "请再帮我看一下")
        await channel._on_message(manual)
        forwarded.assert_awaited_once_with(manual)
    finally:
        await channel.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("same_chat", [False, True])
async def test_late_old_ticket_notice_uses_its_chat_before_observing_new_ticket(
    desk, monkeypatch, legacy, same_chat
):
    service, api, clock = await initialized(desk)
    service.judge.judge.return_value = Decision("resolved", reason="业务确认恢复")
    send = api.cli.send_text

    async def lost_notice(*args, **kwargs):
        await send(*args, **kwargs)
        raise LarkCLIError("response lost", retryable=True)

    api.cli.send_text = lost_notice
    human(service, clock)
    await drain(service)
    out = service.store.pending_sends("t1")[0]
    assert out["kind"] == "notice" and service.store.tickets["t1"]["status"] == 51
    sent = api.cli.receipts[out["uuid"]]
    if legacy:
        del out["chat_id"]
        service.store.save_outbox("t1:notice", out)
    clock.advance(1)
    item = add_ticket(service, api, clock, "t2", chat="oc_ticket" if same_chat else "oc_other")
    service.store.save(item, count=1, last_send=clock())
    channel = FeishuHelpdeskChannel(service.config, service.bus)
    channel._followup = service
    forwarded = AsyncMock()
    monkeypatch.setattr(FeishuChannel, "_on_message", forwarded)
    event = native(clock, sent["message_id"], out["text"])
    event.event.message.chat_id = service.store.tickets["t2"]["chat_id"]
    event.event.message.create_time = str(sent["create_time"])
    try:
        await channel._on_message(event)
        current = service.store.tickets["t2"]
        if same_chat:
            forwarded.assert_not_awaited()
            assert current["generation"] == item["generation"] and current["count"] == 1
            assert not current["last_human"]
            assert not service.store.pending_sends("t1")
            assert not service.store.tickets["t1"]["notice_pending"]
            assert service.store.outbox("t1:notice")["message_id"] == sent["message_id"]
            manual = native(clock, "om_new_ticket_manual", "我在跟进新问题")
            await channel._on_message(manual)
            forwarded.assert_awaited_once_with(manual)
            assert service.store.tickets["t2"]["generation"] == item["generation"] + 1
        else:
            forwarded.assert_awaited_once_with(event)
            assert current["generation"] == item["generation"] + 1
            assert service.store.pending_sends("t1")
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_identical_pending_sends_in_one_chat_do_not_settle_the_wrong_ticket(desk):
    service, api, clock, out = await paused(desk)
    clock.advance(1)
    item = add_ticket(service, api, clock, "t2", chat="oc_ticket")
    second = {**out, "ticket": "t2", "generation": item["generation"], "uuid": "second-send"}
    key = f"t2:{out['kind']}"
    service.store.save_outbox(key, second)
    event = native(clock, "om_ambiguous_echo", out["text"])
    assert service.native(event) == "drop"
    assert service.store.tickets["t2"]["generation"] == item["generation"]
    assert service.store.pending_sends("t1") and service.store.pending_sends("t2")
    assert service.store.tickets["t2"]["count"] == 0


@pytest.mark.asyncio
async def test_prefix_or_another_sender_cannot_impersonate_automatic_messages(desk):
    service, api, clock, out = await paused(desk)
    assert service.native(native(clock, "om_prefixed", "【服务台自动消息】\n我手动补充")) == "pass"
    assert service.native(native(clock, "om_other", out["text"], sender="ou_other")) == "pass"
    records = {m["im_id"]: m for m in service.store.messages("t1")}
    assert records["om_prefixed"]["human"] is True and records["om_other"]["human"] is True
    assert service.store.pending_sends("t1")


@pytest.mark.asyncio
async def test_receipt_pagination_ignores_other_targets_and_known_receipts(desk):
    service, api, clock, out = await paused(desk)
    api.cli.chat_messages.side_effect = [
        {
            "items": [receipt(out, chat="oc_unrelated"), receipt(out, "om_auto1")],
            "has_more": True,
            "page_token": "next",
        },
        {"items": [receipt(out)], "has_more": False},
    ]
    await service.delivery.reconcile()
    assert api.cli.chat_messages.call_args.args[-1] == "next"
    assert service.store.outbox(f"t1:{out['kind']}")["message_id"] == "om_recovered"


@pytest.mark.asyncio
async def test_bad_receipt_page_leaves_pause_and_does_not_count_or_send(desk):
    service, api, clock, out = await paused(desk)
    api.cli.chat_messages.return_value = {"items": [], "has_more": True, "page_token": ""}
    await service.delivery.reconcile()
    assert service.store.tickets["t1"]["paused"]
    assert service.store.tickets["t1"]["count"] == 0
    assert len(api.cli.sends) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("elapsed,wait", [(60, 0), (3490, 120)])
async def test_receipt_found_during_cli_lock_wait_is_not_overwritten_or_resent(desk, elapsed, wait):
    service, api, clock = await initialized(desk)
    clock.advance(300)
    api.cli.fail_send = True
    await drain(service)
    out = service.store.pending_sends("t1")[0]
    api.cli.fail_send = False
    api.cli.chat_messages.return_value = {"items": [receipt(out)], "has_more": False}

    async def recover():
        clock.advance(wait)
        await service.delivery.reconcile()

    api.cli.before_send = recover
    clock.advance(elapsed)
    # Execute only the waiting retry; an overdue next reminder is separate work.
    await service.tick()
    msg = service.bus.inbound.get_nowait()
    assert await msg.before_process()
    await msg.internal_handler()
    assert len(api.cli.sends) == 1
    assert service.store.outbox(f"t1:{out['kind']}")["message_id"] == "om_recovered"
    assert service.store.tickets["t1"]["count"] == 1
    assert not service.store.tickets["t1"]["paused"]


@pytest.mark.asyncio
async def test_all_automatic_messages_use_configured_format_and_interval(desk):
    service, api, clock = await desk(
        minutes=7,
        messages={
            "prefix": "【自动服务通知】",
            "welcome": "请提供问题详情。",
            "second_reminder_suffix": "还会等待{minutes}分钟，未补充或确认将自动关闭。",
            "closed_timeout": "等待业务回复超时，已关闭。",
        },
    )
    add_ticket(service, api, clock)
    await service.reconcile()
    await drain(service)
    assert api.cli.sends[0][1] == "【自动服务通知】\n请提供问题详情。"
    for _ in range(3):
        clock.advance(420)
        await drain(service)
    assert all(text.startswith("【自动服务通知】\n") for _, text, _ in api.cli.sends)
    assert "等待7分钟" in api.cli.sends[2][1]
    assert api.cli.sends[-1][1] == "【自动服务通知】\n等待业务回复超时，已关闭。"


@pytest.mark.parametrize(
    "messages",
    [
        {"prefix": " "},
        {"welcome": " "},
        {"second_reminder_suffix": "五分钟后关闭"},
        {"second_reminder_suffix": "{other}分钟"},
        {"second_reminder_suffix": "{minutes:01000000}分钟"},
    ],
)
def test_message_configuration_rejects_empty_markers_and_invalid_time_templates(messages):
    with pytest.raises(ValidationError):
        FeishuChannelConfig(helpdesk={"messages": messages})


@pytest.mark.asyncio
async def test_reopen_hint_survives_failure_restart_and_expired_discovery_window(desk, tmp_path):
    service, api, clock = await initialized(desk)
    service.judge.judge.return_value = Decision("resolved", reason="业务确认恢复")
    human(service, clock)
    await drain(service)
    assert service.store.tickets["t1"]["status"] == 51
    welcome = service.store.tickets["t1"]["welcome_at"]
    clock.advance(60)
    api.tickets["t1"].update(status=2, updated_at=int(clock() * 1000))
    api.ticket.side_effect = OSError("offline")
    await service.reconcile()
    assert service.store.tickets["t1"]["refresh_pending"]
    assert service.store.tickets["t1"]["status"] == 51
    await service.close()
    clock.advance(3600)
    restarted, _, _ = await desk(path=tmp_path, clock=clock, api=api)
    # The update is now absent from discovery; its durable refresh hint remains.
    api.tickets_page.side_effect = None
    api.tickets_page.return_value = {"tickets": [], "total": 0}
    api.ticket.side_effect = lambda tid: copy.deepcopy(api.tickets[tid])
    await restarted.reconcile()
    item = restarted.store.tickets["t1"]
    assert item["status"] == 2 and not item["refresh_pending"]
    assert item["welcome_at"] == welcome and item["count"] == 0
    restarted.judge.judge.return_value = Decision("resolved", reason="旧消息已解决")
    await drain(restarted)
    assert len(api.cli.closes) == 1 and len(api.cli.sends) == 2
    clock.advance(1)
    human(restarted, clock, "om_new_resolution")
    await drain(restarted)
    assert len(api.cli.closes) == 2 and len(api.cli.sends) == 3
    assert api.cli.sends[-1][2] != api.cli.sends[-2][2]


@pytest.mark.asyncio
async def test_new_ticket_after_closure_has_its_own_welcome(desk):
    service, api, clock = await initialized(desk)
    service.judge.judge.return_value = Decision("resolved", reason="已解决")
    human(service, clock)
    await drain(service)
    clock.advance(60)
    add_ticket(service, api, clock, "t2", chat="oc_new_ticket")
    await service.reconcile()
    await drain(service)
    assert len([text for _, text, _ in api.cli.sends if "请描述遇到的问题" in text]) == 2
    assert api.cli.sends[-1][0] == "oc_new_ticket"


@pytest.mark.asyncio
async def test_human_message_before_reopen_scan_counts_as_new_communication(desk):
    service, api, clock = await initialized(desk)
    service.judge.judge.return_value = Decision("resolved", reason="已解决")
    human(service, clock)
    await drain(service)
    clock.advance(60)
    api.tickets["t1"].update(status=2, updated_at=int(clock() * 1000))
    clock.advance(5)
    api.messages["t1"] = [
        {
            "id": "tm_reopened_human",
            "message_id": "om_reopened_human",
            "created_at": int(clock() * 1000),
            "user_id": "ou_guest",
            "message_type": "text",
            "content": "出现了新的问题",
        }
    ]
    api.cli.message.side_effect = None
    api.cli.message.return_value = {
        "chat_id": "oc_ticket",
        "sender": {"sender_type": "user", "id": "ou_guest"},
    }
    clock.advance(10)
    service.judge.judge.reset_mock()
    service.judge.judge.return_value = Decision("awaiting_handler", "请跟进新问题。")
    await service.reconcile()
    await drain(service)
    assert not service.store.tickets["t1"]["reopen_waiting_for_human"]
    service.judge.judge.assert_awaited_once()
    assert len(api.cli.closes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("close_delay", [0, 60])
async def test_preclose_history_cannot_unlock_reopened_ticket(desk, tmp_path, restart, close_delay):
    service, api, clock = await initialized(desk)
    clock.advance(60)
    api.messages["t1"] = [
        {
            "id": "tm_before_close",
            "message_id": "om_before_close",
            "created_at": int(clock() * 1000),
            "content": "问题已经解决，可以关闭",
            "user_id": "ou_guest",
            "message_type": "text",
        }
    ]
    clock.advance(close_delay)
    api.tickets["t1"].update(status=51, updated_at=int(clock() * 1000))
    # The close event won the race with polling; its earlier human message
    # must not later masquerade as communication after reopening.
    service.register(copy.deepcopy(api.tickets["t1"]))
    assert not service.store.messages("t1")
    if restart:
        await service.close()
        service, _, _ = await desk(path=tmp_path, clock=clock, api=api)
    clock.advance(60)
    api.tickets["t1"].update(status=2, updated_at=int(clock() * 1000))
    api.cli.message.side_effect = None
    api.cli.message.return_value = {
        "chat_id": "oc_ticket",
        "sender": {"sender_type": "user", "id": "ou_guest"},
    }
    service.judge.judge.reset_mock()
    service.judge.judge.return_value = Decision("resolved", reason="旧对话已解决")
    await service.reconcile()
    await drain(service)
    item = service.store.tickets["t1"]
    assert item["status"] == 2 and item["reopen_waiting_for_human"]
    assert not api.cli.closes and len(api.cli.sends) == 1
    service.judge.judge.assert_not_awaited()
    clock.advance(1)
    human(service, clock, "om_after_reopen", text="又遇到了新的问题")
    service.judge.judge.return_value = Decision("awaiting_handler", "请处理方跟进新问题。")
    await drain(service)
    assert not service.store.tickets["t1"]["reopen_waiting_for_human"]
    service.judge.judge.assert_awaited_once()
    assert not api.cli.closes
