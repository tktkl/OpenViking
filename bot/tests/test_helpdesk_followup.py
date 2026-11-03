"""Durable Helpdesk automation with a wall clock and fully offline user/API adapters."""

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from vikingbot.bus.queue import MessageBus
from vikingbot.config.schema import FeishuChannelConfig
from vikingbot.extensions.feishu_helpdesk.cli import LarkCLIError
from vikingbot.extensions.feishu_helpdesk.decision import WELCOME, ConversationJudge, Decision
from vikingbot.extensions.feishu_helpdesk.followup import HelpdeskFollowup
from vikingbot.extensions.feishu_helpdesk.state import HelpdeskState
from vikingbot.providers.base import LLMResponse


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeCLI:
    def __init__(self, api, clock):
        self.api, self.clock = api, clock
        self.receipts, self.sends, self.closes = {}, [], []
        self.fail_send = False
        self.before_send = self.before_close = None
        self.message = AsyncMock(side_effect=LarkCLIError("unavailable"))

    async def send_text(self, chat, text, key, *, before_call):
        if self.before_send:
            await self.before_send()
        await before_call("ou_operator")
        if self.fail_send:
            raise LarkCLIError("timed out", retryable=True)
        if key not in self.receipts:
            self.sends.append((chat, text, key))
            self.receipts[key] = {
                "message_id": f"om_auto{len(self.sends)}",
                "create_time": int(self.clock() * 1000),
            }
        return self.receipts[key]

    async def close_ticket(self, ticket, authorization, body, *, before_call):
        if self.before_close:
            await self.before_close()
        await before_call("ou_operator")
        self.closes.append((ticket, copy.deepcopy(body)))
        self.api.tickets[ticket].update(
            status=51,
            solve=body["solved"],
            comments={"content": body["comment"]},
            updated_at=int(self.clock() * 1000),
        )
        return {}


class FakeAPI:
    authorization = "fake-desk-header"

    def __init__(self, clock):
        self.clock, self.tickets, self.messages = clock, {}, {}
        self.cli = FakeCLI(self, clock)
        self.ticket = AsyncMock(side_effect=lambda tid: copy.deepcopy(self.tickets[tid]))
        self.messages_page = AsyncMock(side_effect=self._messages)
        self.tickets_page = AsyncMock(side_effect=self._tickets)

    def _tickets(self, field, start, end, page):
        rows = [
            copy.deepcopy(t)
            for t in self.tickets.values()
            if start
            <= t.get({"create": "created_at", "update": "updated_at"}[field], t["created_at"])
            / 1000
            <= end
        ]
        return {"tickets": rows, "total": len(rows)}

    def _messages(self, tid, start, end, page):
        rows = [
            copy.deepcopy(m)
            for m in self.messages.get(tid, [])
            if start <= m["created_at"] / 1000 <= end
        ]
        return {"messages": rows, "total": len(rows)}


@pytest.fixture
async def desk(tmp_path):
    services = []

    async def create(*, minutes=5, path=None, clock=None, api=None, bus=None, session_busy=None):
        clock = clock or Clock()
        config = FeishuChannelConfig(
            app_id="cli_bot",
            helpdesk={
                "enabled": True,
                "helpdesk_id": "desk",
                "helpdesk_token": "fake",
                "followup_interval_minutes": minutes,
            },
        )
        api = api or FakeAPI(clock)
        service = HelpdeskFollowup(
            config,
            bus or MessageBus(),
            api,
            path or tmp_path,
            clock=clock,
            session_busy=session_busy,
        )
        service.judge.judge = AsyncMock(return_value=Decision("awaiting_details", "请补充问题。"))
        await service.start(background=False)
        services.append(service)
        return service, api, clock

    yield create
    for service in services:
        await service.close()


def add_ticket(service, api, clock, tid="t1", *, chat="oc_ticket", created=None):
    ticket = {
        "ticket_id": tid,
        "helpdesk_id": "desk",
        "status": 2,
        "created_at": int((created if created is not None else clock()) * 1000),
        "guest": {"id": "ou_guest"},
        "agents": [{"id": "ou_agent"}],
        "collaborators": [],
        "updated_at": int(clock() * 1000),
    }
    api.tickets[tid] = ticket
    return service.register(ticket, chat_id=chat)


def human(service, clock, mid="om_h1", tid="t1", **extra):
    service.observe(
        tid,
        {
            "im_id": mid,
            "sender": "ou_guest",
            "human": True,
            "time": clock(),
            "text": "请帮忙",
            "kind": "text",
            **extra,
        },
    )


async def drain(service, *, rounds=5):
    """Execute the real queued guards/handlers without creating an extra worker pool."""
    for _ in range(rounds):
        await service.tick()
        if service.bus.inbound.empty():
            return
        while not service.bus.inbound.empty():
            msg = service.bus.inbound.get_nowait()
            assert msg.internal and not msg.need_reply
            if await msg.before_process():
                await msg.internal_handler()
    raise AssertionError("automation did not become idle")


async def initialized(desk, **kwargs):
    service, api, clock = await desk(**kwargs)
    add_ticket(service, api, clock)
    await service.reconcile()
    await drain(service)
    assert api.cli.sends[0][1] == WELCOME
    return service, api, clock


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "5", None])
def test_interval_is_a_strict_positive_integer(value):
    with pytest.raises(ValidationError):
        FeishuChannelConfig(helpdesk={"followup_interval_minutes": value})


def test_interval_defaults_to_five_minutes():
    assert FeishuChannelConfig().helpdesk.followup_interval_minutes == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario,should_close",
    [
        ("awaiting_details", True),
        ("awaiting_confirmation", True),
        ("awaiting_handler", False),
    ],
)
async def test_five_ten_fifteen_minute_timeline(desk, scenario, should_close):
    service, api, clock = await initialized(desk)
    service.judge.judge.return_value = Decision(scenario, "请跟进当前问题。")
    human(service, clock)
    await drain(service)
    clock.advance(299)
    await drain(service)
    assert len(api.cli.sends) == 1
    clock.advance(1)
    await drain(service)
    assert len(api.cli.sends) == 2
    clock.advance(300)
    await drain(service)
    assert len(api.cli.sends) == 3
    if should_close:
        assert "5分钟后将自动关闭" in api.cli.sends[-1][1]
    else:
        assert "自动关闭" not in api.cli.sends[-1][1]
    clock.advance(300)
    await drain(service)
    assert bool(api.cli.closes) == should_close
    if should_close:
        assert api.cli.closes[0][1]["solved"] == 1
        assert api.cli.sends[-1][1].endswith("工单已关闭。")
    else:
        clock.advance(3600)
        await drain(service)
        assert len(api.cli.sends) == 3


@pytest.mark.asyncio
async def test_send_success_time_controls_next_deadline_no_catchup(desk):
    service, api, clock = await initialized(desk, minutes=7)
    clock.advance(3600)
    await drain(service)
    assert service.store.tickets["t1"]["count"] == 1
    clock.advance(419)
    await drain(service)
    assert len(api.cli.sends) == 2
    clock.advance(1)
    await drain(service)
    assert "7分钟后将自动关闭" in api.cli.sends[-1][1]


@pytest.mark.asyncio
async def test_failure_does_not_count_or_start_close_wait(desk):
    service, api, clock = await initialized(desk)
    clock.advance(300)
    api.cli.fail_send = True
    await drain(service)
    item = service.store.tickets["t1"]
    assert item["count"] == 0 and item["last_send"] is None
    api.cli.fail_send = False
    clock.advance(60)
    await drain(service)
    assert service.store.tickets["t1"]["count"] == 1
    assert service.store.tickets["t1"]["last_send"] == clock()


@pytest.mark.asyncio
async def test_human_resets_immediately_even_for_media_and_older_delivery(desk):
    service, api, clock = await initialized(desk)
    clock.advance(300)
    await drain(service)
    clock.advance(100)
    human(service, clock, kind="image", text="")
    item = service.store.tickets["t1"]
    assert item["count"] == 0 and item["last_send"] is None
    generation = item["generation"]
    human(service, clock, "om_older", time=clock() - 30)
    assert service.store.tickets["t1"]["generation"] == generation + 1
    assert service.store.tickets["t1"]["last_human"] == clock()
    await drain(service)
    clock.advance(299)
    await drain(service)
    assert len(api.cli.sends) == 2


@pytest.mark.asyncio
async def test_old_queued_action_is_rejected_before_model(desk):
    service, api, clock = await initialized(desk)
    clock.advance(300)
    await service.tick()
    msg = service.bus.inbound.get_nowait()
    calls = service.judge.judge.await_count
    human(service, clock)
    assert await msg.before_process() is False
    assert service.judge.judge.await_count == calls
    assert not service._queued


@pytest.mark.asyncio
async def test_human_arriving_during_model_invalidates_result(desk):
    service, api, clock = await initialized(desk)
    clock.advance(300)

    async def judge(*args):
        human(service, clock)
        return Decision("resolved", reason="完成")

    service.judge.judge.side_effect = judge
    await service.tick()
    msg = service.bus.inbound.get_nowait()
    await msg.internal_handler()
    assert not api.cli.closes and len(api.cli.sends) == 1


@pytest.mark.asyncio
async def test_human_during_cli_lock_wait_prevents_send(desk):
    service, api, clock = await initialized(desk)
    clock.advance(300)

    async def incoming():
        human(service, clock)

    api.cli.before_send = incoming
    await service.tick()
    msg = service.bus.inbound.get_nowait()
    await msg.internal_handler()
    assert len(api.cli.sends) == 1
    assert service.store.tickets["t1"]["count"] == 0


@pytest.mark.asyncio
async def test_latest_api_message_during_cli_auth_wait_prevents_close(desk):
    service, api, clock = await initialized(desk)
    service.judge.judge.return_value = Decision("resolved", reason="沟通已解决")
    human(service, clock)

    async def incoming():
        api.messages["t1"] = [
            {
                "id": "tm_new",
                "message_id": "om_latest",
                "created_at": int(clock() * 1000),
                "content": "还有问题",
                "user_id": "ou_guest",
                "message_type": "text",
            }
        ]
        api.cli.message.return_value = {
            "chat_id": "oc_ticket",
            "sender": {"sender_type": "user", "id": "ou_guest"},
        }
        api.cli.message.side_effect = None

    api.cli.before_close = incoming
    await service.tick()
    msg = service.bus.inbound.get_nowait()
    await msg.internal_handler()
    assert not api.cli.closes


@pytest.mark.asyncio
async def test_final_check_cannot_deadlock_while_real_cli_and_sync_locks_are_held(desk):
    from test_feishu_helpdesk_cli import authorization, success
    from vikingbot.extensions.feishu_helpdesk.cli import LarkCLIAdapter
    from vikingbot.extensions.feishu_helpdesk.config import LarkCLIConfig
    from vikingbot.extensions.feishu_helpdesk.followup import StaleAction

    service, api, clock = await initialized(desk)
    adapter = LarkCLIAdapter(LarkCLIConfig(), "cli_bot")
    adapter._command = AsyncMock(
        side_effect=lambda *args, **kwargs: authorization() if args[0] == "auth" else success()
    )
    api.cli = adapter
    api.messages["t1"] = [
        {
            "id": "tm_late",
            "message_id": "om_late",
            "created_at": int(clock() * 1000),
            "content": "还有问题",
            "user_id": "ou_guest",
        }
    ]
    generation = service.store.tickets["t1"]["generation"]

    async def final_check(_actor):
        assert adapter._lock.locked()
        await service.sync.latest("t1")
        service._check("t1", generation)

    # The polling task may own this lock while it is itself waiting for CLI.
    async with service.sync.locks["t1"]:
        with pytest.raises(StaleAction):
            await asyncio.wait_for(
                adapter.close_ticket("t1", "fake", {"status": 51}, before_call=final_check), 1
            )
    assert adapter._command.await_count == 1  # local auth only; no IM lookup/PUT inside lock


@pytest.mark.asyncio
async def test_immediate_resolution_and_busy_protection(desk):
    busy = [True]
    service, api, clock = await initialized(desk, session_busy=lambda _: busy[0])
    service.judge.judge.return_value = Decision("resolved", reason="业务已确认恢复")
    human(service, clock)
    await drain(service)
    assert not api.cli.closes
    busy[0] = False
    clock.advance(10)
    await drain(service)
    assert api.cli.closes[0][1]["solved"] == 2


@pytest.mark.asyncio
async def test_alias_dedup_and_automatic_messages_never_reset(desk):
    service, api, clock = await initialized(desk)
    human(service, clock, ticket_id="tm_h1")
    generation = service.store.tickets["t1"]["generation"]
    human(service, clock, "", ticket_id="tm_h1")
    human(service, clock)
    assert service.store.tickets["t1"]["generation"] == generation
    human(service, clock, "om_auto1")
    service.observe(
        "t1", {"ticket_id": "tm_system", "human": False, "time": clock(), "text": "系统消息"}
    )
    assert service.store.tickets["t1"]["generation"] == generation
    assert len(service.store.messages("t1")) == 3


@pytest.mark.asyncio
async def test_welcome_and_receipts_survive_restart(desk, tmp_path):
    service, api, clock = await initialized(desk)
    generation = service.store.tickets["t1"]["generation"]
    await service.close()
    restarted, _, _ = await desk(path=tmp_path, clock=clock, api=api)
    await restarted.tick()
    assert restarted.bus.inbound.empty()  # startup reconciliation gates actions
    await restarted.reconcile()
    await drain(restarted)
    assert len(api.cli.sends) == 1
    human(restarted, clock, "om_auto1")
    assert restarted.store.tickets["t1"]["generation"] == generation


@pytest.mark.asyncio
async def test_uncertain_receipt_reuses_uuid_after_restart(desk, tmp_path):
    service, api, clock = await desk()
    add_ticket(service, api, clock)
    api.cli.fail_send = True
    await service.reconcile()
    await drain(service)
    pending = service.store.pending_sends("t1")[0]
    # Remote accepted the message, but the caller lost the response.
    api.cli.receipts[pending["uuid"]] = {
        "message_id": "om_accepted",
        "create_time": int(clock() * 1000),
    }
    await service.close()
    api.cli.fail_send = False
    clock.advance(60)
    restarted, _, _ = await desk(path=tmp_path, clock=clock, api=api)
    await restarted.reconcile()
    await drain(restarted)
    assert restarted.store.automatic("om_accepted")
    assert not api.cli.sends


@pytest.mark.asyncio
async def test_uncertain_old_round_pauses_instead_of_sending_stale_reminder(desk):
    service, api, clock = await initialized(desk)
    clock.advance(300)
    api.cli.fail_send = True
    await drain(service)
    human(service, clock)
    api.cli.fail_send = False
    await drain(service)
    assert service.store.tickets["t1"]["paused"]
    assert len(api.cli.sends) == 1


@pytest.mark.asyncio
async def test_initial_activation_filters_history_and_recovery_discovers_new(desk):
    service, api, clock = await desk()
    assert add_ticket(service, api, clock, "old", created=clock() - 1) is None
    clock.advance(1)
    new = add_ticket(service, api, clock, "new", chat="")
    del service.store.tickets["new"]
    service.store.db.execute("DELETE FROM tickets WHERE id='new'")
    await service.reconcile()
    assert "old" not in service.store.tickets and "new" in service.store.tickets
    assert not service.store.tickets["new"]["chat_id"]
    assert new["created"] == clock()


@pytest.mark.asyncio
async def test_incomplete_sync_never_advances_cursor_or_runs_actions(desk):
    service, api, clock = await desk()
    item = add_ticket(service, api, clock)
    api.messages_page.return_value = {"messages": [], "total": 1}
    api.messages_page.side_effect = None
    clock.advance(500)
    await service.reconcile()
    await service.tick()
    assert service.store.tickets["t1"]["message_cursor"] == item["message_cursor"]
    assert not service._synced and service.bus.inbound.empty()


@pytest.mark.asyncio
async def test_unverified_history_blocks_model_and_close(desk):
    service, api, clock = await desk()
    add_ticket(service, api, clock)
    api.messages["t1"] = [
        {
            "id": "tm_opaque",
            "created_at": int(clock() * 1000),
            "user_id": "ou_guest",
            "content": "系统通知",
        }
    ]
    await service.reconcile()
    service.judge = ConversationJudge(None, "test")
    await drain(service)
    clock.advance(3600)
    await drain(service)
    assert not api.cli.closes
    assert len(api.cli.sends) == 1


@pytest.mark.asyncio
async def test_shutdown_cancels_execution_before_closing_database(desk):
    service, api, clock = await initialized(desk)
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def blocked(*args):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            assert service.store._owner is not None
            cleaned.set()

    service.judge.judge.side_effect = blocked
    human(service, clock)
    await service.tick()
    msg = service.bus.inbound.get_nowait()
    task = asyncio.create_task(msg.internal_handler())
    await entered.wait()
    await asyncio.wait_for(service.close(), 1)
    await task
    assert cleaned.is_set() and service.store._owner is None
    assert await msg.before_process() is False


def test_closed_ticket_cannot_be_reopened_by_late_create(tmp_path):
    clock = Clock()
    state = HelpdeskState(tmp_path / "state.sqlite3", clock())
    try:
        ticket = {"ticket_id": "t1", "created_at": int(clock() * 1000), "status": 1}
        state.register(ticket, clock())
        state.register({**ticket, "status": 51, "updated_at": int((clock() + 1) * 1000)}, clock())
        assert state.register(ticket, clock())["status"] == 51
        newer = {**ticket, "updated_at": int((clock() + 2) * 1000)}
        assert state.register(newer, clock())["status"] == 51
        assert state.register(newer, clock(), authoritative=True)["status"] == 1
    finally:
        state.close()


@pytest.mark.asyncio
async def test_judge_has_independent_budget_and_requires_latest_evidence():
    provider = SimpleNamespace(
        chat=AsyncMock(
            return_value=LLMResponse(
                content=json.dumps(
                    {
                        "scenario": "resolved",
                        "evidence_ids": ["om_old"],
                        "reason": "已解决",
                        "followup": "",
                    }
                )
            )
        )
    )
    judge = ConversationJudge(provider, "model")
    messages = [
        {
            "im_id": f"om_{i}",
            "time": i,
            "human": True,
            "sender": "ou_guest",
            "text": "问题描述" * 1000,
        }
        for i in range(20)
    ]
    provider.chat.return_value.content = json.dumps(
        {"scenario": "resolved", "evidence_ids": [], "reason": "好了"}
    )
    result = await judge.judge({"id": "t1", "guest": {"id": "ou_guest"}}, messages)
    assert result.scenario == "unknown"
    call = provider.chat.call_args.kwargs
    assert not call["tools"] and not call["thinking"]
    from openviking.utils.token_estimation import estimate_serialized_tokens

    assert estimate_serialized_tokens(call["messages"]) <= 3000


@pytest.mark.asyncio
async def test_discovery_cursors_overlap_and_advance_independently(desk):
    service, api, clock = await desk()
    activated = clock()
    clock.advance(120)

    async def pages(field, start, end, page):
        if field == "update":
            raise OSError("offline")
        return {"tickets": [], "total": 0}

    api.tickets_page.side_effect = pages
    await service.reconcile()
    assert service.store.meta("create_cursor") == clock()
    assert service.store.meta("update_cursor") == activated
    api.tickets_page.reset_mock()
    api.tickets_page.side_effect = None
    api.tickets_page.return_value = {"tickets": [], "total": 0}
    clock.advance(30)
    await service.reconcile()
    queries = [c.args for c in api.tickets_page.call_args_list]
    assert queries[0] == ("create", activated + 60, clock(), 1)
    assert queries[1] == ("update", activated, clock(), 1)


@pytest.mark.asyncio
async def test_large_discovery_window_is_split_before_ten_thousand_cap(desk):
    service, api, clock = await desk()
    clock.advance(100)
    windows = []

    async def pages(field, start, end, page):
        windows.append((field, start, end))
        return {"tickets": [], "total": 10000 if end - start > 50 else 0}

    api.tickets_page.side_effect = pages
    await service.sync.discover()
    assert len(windows) == 6
    assert service.store.meta("create_cursor") == clock()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "second",
    [
        {"tickets": [], "total": 2},
        {"tickets": [], "total": 1},
        {"tickets": [{"ticket_id": "t1"}], "total": 2},
    ],
)
async def test_partial_or_unstable_pagination_does_not_advance_ticket_cursor(desk, second):
    service, api, clock = await desk()
    item = add_ticket(service, api, clock)
    cursor = service.store.meta("create_cursor")
    clock.advance(60)
    api.tickets_page.side_effect = [{"tickets": [api.tickets["t1"]], "total": 2}, second]
    with pytest.raises(ValueError):
        await service.sync.discover()
    assert service.store.meta("create_cursor") == cursor
    assert item["id"] in service.store.tickets


@pytest.mark.asyncio
async def test_message_cursor_is_independent_of_ticket_updated_at_and_recovers_chat(desk):
    service, api, clock = await desk()
    add_ticket(service, api, clock, chat="")
    initial_updated = api.tickets["t1"]["updated_at"]
    clock.advance(70)
    api.messages["t1"] = [
        {
            "id": "tm_recovered",
            "message_id": "om_recovered",
            "created_at": int((clock() - 30) * 1000),
            "content": "问题截图",
            "user_id": "ou_guest",
            "message_type": "image",
        }
    ]
    api.cli.message.side_effect = None
    api.cli.message.return_value = {
        "message_id": "om_recovered",
        "chat_id": "oc_ticket",
        "sender": {"sender_type": "user", "id": "ou_guest"},
        "msg_type": "image",
    }
    service.on_sync = AsyncMock()
    await service.reconcile()
    item = service.store.tickets["t1"]
    assert item["chat_id"] == "oc_ticket" and item["last_human"] == clock() - 30
    assert item["message_cursor"] == clock()
    assert api.tickets["t1"]["updated_at"] == initial_updated
    service.on_sync.assert_awaited_once_with(item)


@pytest.mark.asyncio
async def test_old_unknown_message_and_binding_recover_after_permissions_are_fixed(desk):
    service, api, clock = await desk()
    add_ticket(service, api, clock, chat="")
    api.messages["t1"] = [
        {
            "id": "tm_old",
            "message_id": "om_old",
            "created_at": int(clock() * 1000),
            "content": "问题",
            "user_id": "ou_guest",
        }
    ]
    await service.reconcile()
    assert not service.store.tickets["t1"]["chat_id"]
    clock.advance(300)
    api.cli.message.side_effect = None
    api.cli.message.return_value = {
        "chat_id": "oc_ticket",
        "sender": {"id": "ou_guest", "sender_type": "user"},
    }
    await service.reconcile()
    assert service.store.tickets["t1"]["chat_id"] == "oc_ticket"
    assert service.store.messages("t1")[0]["human"] is True
    assert service.store.tickets["t1"]["last_human"] == clock() - 300


@pytest.mark.asyncio
async def test_same_user_manual_message_is_restored_after_automatic_echo(desk):
    service, api, clock = await desk()
    add_ticket(service, api, clock)
    await service.reconcile()
    original_send = api.cli.send_text

    async def send(chat, text, key, *, before_call):
        await before_call("ou_operator")
        human(service, clock, "om_auto1", sender="ou_operator")
        human(service, clock, "om_manual", sender="ou_operator", text="我来处理")
        return await original_send(chat, text, key, before_call=before_call)

    api.cli.send_text = send
    await drain(service)
    messages = {m["im_id"]: m for m in service.store.messages("t1")}
    assert messages["om_auto1"]["human"] is False
    assert messages["om_manual"]["human"] is True
    assert service.store.tickets["t1"]["last_human"] == clock()


@pytest.mark.asyncio
async def test_automatic_cli_echo_during_reminder_does_not_invalidate_its_receipt(desk):
    service, api, clock = await initialized(desk)
    generation = service.store.tickets["t1"]["generation"]
    original_send = api.cli.send_text

    async def echo(chat, text, key, *, before_call):
        await before_call("ou_operator")
        human(service, clock, "om_auto2", sender="ou_operator")
        return await original_send(chat, text, key, before_call=before_call)

    api.cli.send_text = echo
    clock.advance(300)
    await drain(service)
    assert len(api.cli.sends) == 2
    assert service.store.tickets["t1"]["count"] == 1
    assert service.store.tickets["t1"]["generation"] == generation


@pytest.mark.asyncio
async def test_close_and_notice_recover_after_lost_response(desk, tmp_path):
    service, api, clock = await initialized(desk)
    service.judge.judge.return_value = Decision("resolved", reason="业务确认恢复")
    human(service, clock)
    close = api.cli.close_ticket

    async def lost(*args, **kwargs):
        await close(*args, **kwargs)
        raise LarkCLIError("timed out", retryable=True)

    api.cli.close_ticket = lost
    await drain(service)
    assert service.store.tickets["t1"]["close_pending"]
    await service.close()
    clock.advance(60)
    restarted, _, _ = await desk(path=tmp_path, clock=clock, api=api)
    await restarted.reconcile()
    await drain(restarted)
    assert len(api.cli.closes) == 1 and len(api.cli.sends) == 2
    assert api.cli.sends[-1][1] == "问题已解决，工单已关闭。"


@pytest.mark.asyncio
async def test_unknown_model_result_and_failure_keep_ticket_open(desk):
    service, api, clock = await initialized(desk)
    human(service, clock)
    service.judge.judge.return_value = Decision("unknown")
    clock.advance(1000)
    await drain(service)
    service.judge.judge.side_effect = ValueError("bad JSON")
    clock.advance(60)
    await drain(service)
    assert not api.cli.closes and len(api.cli.sends) == 1


@pytest.mark.asyncio
async def test_nearest_deadline_uses_wall_time(desk):
    service, api, clock = await initialized(desk)
    clock.advance(297)
    assert service._next_wait() == 3
    clock.advance(-100)
    assert service._next_wait() == 10


@pytest.mark.asyncio
async def test_real_ticket_message_event_and_native_im_merge_without_duplicate_reset(
    desk, monkeypatch
):
    from lark_oapi.api.helpdesk.v1 import P2HelpdeskTicketMessageCreatedV1
    from test_feishu_helpdesk import im_event
    from vikingbot.channels.feishu import FeishuChannel
    from vikingbot.extensions.feishu_helpdesk.channel import FeishuHelpdeskChannel

    service, api, clock = await desk()
    add_ticket(service, api, clock)
    channel = FeishuHelpdeskChannel(service.config, service.bus)
    channel._followup = service
    native = AsyncMock()
    monkeypatch.setattr(FeishuChannel, "_on_message", native)
    event = P2HelpdeskTicketMessageCreatedV1(
        {
            "schema": "2.0",
            "header": {"app_id": "cli_bot", "create_time": str(int(clock() * 1000))},
            "event": {
                "ticket_message_id": "tm_event",
                "message_id": "om_user",
                "sender_type": 2,
                "sender_id": {"open_id": "ou_user"},
                "ticket": {"ticket_id": "t1"},
                "chat_id": "oc_ticket",
                "msg_type": "text",
                "text": "@_user_1 帮我排查",
            },
        }
    )
    try:
        await channel._on_ticket_message(event)
        generation = service.store.tickets["t1"]["generation"]
        await channel._on_ticket_message(event)
        msg = im_event()
        msg.event.message.create_time = str(int(clock() * 1000))
        await channel._on_message(msg)
        assert service.store.tickets["t1"]["generation"] == generation
        assert len(service.store.messages("t1")) == 1
        native.assert_awaited_once_with(msg)  # Helpdesk event is monitor-only
        event.event.chat_id = "oc_unrelated"
        with pytest.raises(ValueError, match="Conflicting"):
            await channel._on_ticket_message(event)
    finally:
        await channel.stop()


def test_state_is_single_owner_and_can_merge_two_previously_distinct_aliases(tmp_path):
    path, clock = tmp_path / "state.sqlite3", Clock()
    state = HelpdeskState(path, clock())
    try:
        with pytest.raises(BlockingIOError):
            HelpdeskState(path, clock())
        state.register({"ticket_id": "t", "created_at": int(clock() * 1000), "status": 1}, clock())
        state.observe("t", {"im_id": "om_h", "human": True, "time": clock(), "text": "问题"})
        state.observe("t", {"ticket_id": "tm_h", "human": None, "time": clock(), "text": "问题"})
        state.observe(
            "t",
            {"im_id": "om_h", "ticket_id": "tm_h", "human": True, "time": clock(), "text": "问题"},
        )
        assert len(state.messages("t")) == 1
    finally:
        state.close()
