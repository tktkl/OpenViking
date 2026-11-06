"""Durable Helpdesk automation with a wall clock and fully offline user/API adapters."""

import asyncio
import copy
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from vikingbot.bus.queue import MessageBus
from vikingbot.config.schema import FeishuChannelConfig
from vikingbot.extensions.feishu_helpdesk.cli import LarkCLIError
from vikingbot.extensions.feishu_helpdesk.decision import WELCOME, ConversationJudge, Decision
from vikingbot.extensions.feishu_helpdesk.followup import HelpdeskFollowup, StaleAction
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
        self.chat_messages = AsyncMock(return_value={"items": [], "has_more": False})

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
        self.send_bot_text = AsyncMock(side_effect=self._send_bot_text)
        self.ticket = AsyncMock(side_effect=lambda tid: copy.deepcopy(self.tickets[tid]))
        self.messages_page = AsyncMock(side_effect=self._messages)
        self.tickets_page = AsyncMock(side_effect=self._tickets)

    async def _send_bot_text(self, chat, text, key, *, before_call):
        await before_call("cli_bot")
        if self.cli.fail_send:
            raise LarkCLIError("timed out", retryable=True)
        if key not in self.cli.receipts:
            self.cli.sends.append((chat, text, key))
            self.cli.receipts[key] = {
                "message_id": f"om_bot{len(self.cli.sends)}",
                "create_time": int(self.clock() * 1000),
            }
        return self.cli.receipts[key]

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

    async def create(
        *,
        minutes=5,
        path=None,
        clock=None,
        api=None,
        bus=None,
        session_busy=None,
        messages=None,
        escalation=None,
    ):
        clock = clock or Clock()
        config = FeishuChannelConfig(
            app_id="cli_bot",
            helpdesk={
                "enabled": True,
                "helpdesk_id": "desk",
                "helpdesk_token": "fake",
                "followup_interval_minutes": minutes,
                "messages": messages or {},
                **({"escalation": escalation} if escalation is not None else {}),
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
    assert api.cli.sends[0][1] == service.config.helpdesk.messages.render(WELCOME)
    return service, api, clock


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "5", None])
def test_interval_is_a_strict_positive_integer(value):
    with pytest.raises(ValidationError):
        FeishuChannelConfig(helpdesk={"followup_interval_minutes": value})


def test_interval_defaults_to_three_minutes():
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
async def test_five_minute_reminder_then_timeout_close(desk, scenario, should_close):
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
    assert bool(api.cli.closes) == should_close
    if should_close:
        assert len(api.cli.sends) == 3
        assert api.cli.closes[0][1]["solved"] == 1
        assert "因催办后仍未收到业务补充或确认，工单已关闭。" in api.cli.sends[-1][1]
    else:
        assert len(api.cli.sends) == 2
        assert "自动关闭" not in api.cli.sends[-1][1]
        clock.advance(3600)
        await drain(service)
        assert len(api.cli.sends) == 2


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
    assert "因催办后仍未收到业务补充或确认，工单已关闭。" in api.cli.sends[-1][1]


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
@pytest.mark.parametrize("new_round", [False, True])
async def test_explicit_send_refusal_recovers_after_restart_and_permission_fix(
    desk, tmp_path, monkeypatch, new_round
):
    from test_feishu_helpdesk_cli import auth_access_value, success
    from vikingbot.extensions.feishu_helpdesk.cli import LarkCLIAdapter
    from vikingbot.extensions.feishu_helpdesk.config import LarkCLIConfig

    service, api, clock = await initialized(desk)
    adapter = LarkCLIAdapter(LarkCLIConfig(), "cli_bot")
    adapter._auth_access = AsyncMock(return_value=auth_access_value())
    api.cli = adapter
    permitted = False

    async def spawn(*args, **kwargs):
        response = (
            success(
                {"message_id": "om_after_permission_fix", "create_time": str(int(clock() * 1000))}
            )
            if permitted
            else {"ok": False, "error": {"type": "api", "code": 230035}}
        )
        raw = json.dumps(response).encode()
        return SimpleNamespace(
            returncode=0 if permitted else 1,
            communicate=AsyncMock(return_value=(raw, b"") if permitted else (b"", raw)),
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(side_effect=spawn))
    clock.advance(300)
    await drain(service)
    assert not service.store.pending_sends("t1")
    assert service.store.tickets["t1"]["count"] == 0
    if new_round:
        human(service, clock, "om_human_after_refusal")
        await drain(service)
    assert not service.store.tickets["t1"]["paused"]
    await service.close()
    permitted = True
    restarted, _, _ = await desk(path=tmp_path, clock=clock, api=api)
    await restarted.reconcile()
    clock.advance(300 if new_round else 60)
    await drain(restarted)
    item = restarted.store.tickets["t1"]
    assert not item["paused"] and item["count"] == 1
    assert item["last_send"] == clock() and not restarted.store.pending_sends("t1")


@pytest.mark.asyncio
async def test_refusal_on_retry_does_not_disprove_an_earlier_uncertain_send(desk):
    service, api, clock = await initialized(desk)
    clock.advance(300)
    api.cli.fail_send = True
    await drain(service)
    pending = service.store.pending_sends("t1")[0]

    async def rejected(*args, **kwargs):
        await kwargs["before_call"]("ou_operator")
        raise LarkCLIError("command failed", code=230035, rejected=True)

    api.cli.send_text = rejected
    clock.advance(60)
    await drain(service)
    assert service.store.pending_sends("t1")[0]["attempted_at"] == pending["attempted_at"]
    human(service, clock, "om_new_round")
    await drain(service)
    assert service.store.tickets["t1"]["pause_reason"] == "receipt_from_older_round"


@pytest.mark.asyncio
async def test_remaining_uuid_budget_stops_and_reaps_slow_cli(desk, monkeypatch):
    from test_feishu_helpdesk_cli import auth_access_value, success
    from vikingbot.extensions.feishu_helpdesk.cli import LarkCLIAdapter
    from vikingbot.extensions.feishu_helpdesk.config import LarkCLIConfig

    service, api, clock = await initialized(desk)
    clock.advance(300)
    api.cli.fail_send = True
    await drain(service)
    pending = service.store.pending_sends("t1")[0]
    clock.advance(3499.8)
    adapter = LarkCLIAdapter(
        LarkCLIConfig(executable=sys.executable, timeout_seconds=180), "cli_bot"
    )
    adapter._auth_access = AsyncMock(return_value=auth_access_value())
    api.cli = adapter
    original_spawn, processes = asyncio.create_subprocess_exec, []
    late_receipt = success({"message_id": "om_too_late", "create_time": str(int(clock() * 1000))})

    async def spawn(*args, **kwargs):
        process = await original_spawn(
            sys.executable,
            "-c",
            "import time; time.sleep(0.5); print(" + repr(json.dumps(late_receipt)) + ")",
            **kwargs,
        )
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(LarkCLIError, match="request budget exhausted"):
        await asyncio.wait_for(
            service._send(service.store.tickets["t1"], pending["kind"], pending["text"]), 2
        )
    assert len(processes) == 1 and processes[0].returncode is not None
    if os.name == "posix":
        assert processes[0].returncode < 0
    assert service.store.tickets["t1"]["count"] == 0
    assert service.store.pending_sends("t1")[0]["uuid"] == pending["uuid"]
    clock.advance(1)
    await drain(service)
    assert service.store.tickets["t1"]["pause_reason"] == "receipt_expired"
    assert len(processes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["lock", "authorization"])
async def test_uuid_expiry_is_rechecked_after_cli_wait(desk, phase):
    from test_feishu_helpdesk_cli import auth_access_value, success
    from vikingbot.extensions.feishu_helpdesk.cli import LarkCLIAdapter
    from vikingbot.extensions.feishu_helpdesk.config import LarkCLIConfig

    service, api, clock = await initialized(desk)
    clock.advance(300)
    api.cli.fail_send = True
    await drain(service)
    pending = service.store.pending_sends("t1")[0]
    clock.advance(3490)
    adapter = LarkCLIAdapter(LarkCLIConfig(), "cli_bot")
    api.cli = adapter
    entered, release = asyncio.Event(), asyncio.Event()

    async def auth_access():
        entered.set()
        if phase == "authorization":
            await release.wait()
        return auth_access_value()

    async def command(*args, **kwargs):
        return success({"message_id": "om_expired", "create_time": int(clock() * 1000)})

    adapter._auth_access = AsyncMock(side_effect=auth_access)
    adapter._command = AsyncMock(side_effect=command)
    lock_held = phase == "lock"
    if lock_held:
        await adapter._lock.acquire()
    sending = asyncio.create_task(
        service._send(service.store.tickets["t1"], pending["kind"], pending["text"])
    )
    try:
        if phase == "authorization":
            await asyncio.wait_for(entered.wait(), 2)
        else:
            await asyncio.sleep(0)
            assert not adapter._command.called and not sending.done()
        clock.advance(120)  # The original request is now older than one hour.
        if lock_held:
            adapter._lock.release()
            lock_held = False
        release.set()
        with pytest.raises(LarkCLIError, match="exceeds idempotency window"):
            await asyncio.wait_for(sending, 2)
        assert adapter._command.await_count == 0
        item = service.store.tickets["t1"]
        assert item["paused"] and item["pause_reason"] == "receipt_expired"
        assert item["count"] == 0 and item["last_send"] is None
        assert service.store.pending_sends("t1")[0]["uuid"] == pending["uuid"]
    finally:
        if lock_held:
            adapter._lock.release()
        release.set()
        sending.cancel()
        await asyncio.gather(sending, return_exceptions=True)


@pytest.mark.asyncio
async def test_raw_cli_retry_keeps_original_send_time_for_next_reminder(desk):
    from test_feishu_helpdesk_cli import auth_access_value, success
    from vikingbot.extensions.feishu_helpdesk.cli import LarkCLIAdapter
    from vikingbot.extensions.feishu_helpdesk.config import LarkCLIConfig

    service, api, clock = await initialized(desk)
    adapter = LarkCLIAdapter(LarkCLIConfig(), "cli_bot")
    adapter._auth_access = AsyncMock(return_value=auth_access_value())
    api.cli = adapter
    receipts, requests = {}, []
    lose_response = True

    async def command(*args, **kwargs):
        nonlocal lose_response
        assert args[:4] == ("api", "POST", "/open-apis/im/v1/messages", "--params")
        body = json.loads(args[6])
        requests.append(body)
        if body["uuid"] not in receipts:
            receipts[body["uuid"]] = {
                "message_id": f"om_sent{len(receipts) + 1}",
                "create_time": str(int(clock() * 1000)),
            }
        if lose_response:
            lose_response = False
            raise LarkCLIError("response lost", retryable=True)
        return success(receipts[body["uuid"]])

    adapter._command = AsyncMock(side_effect=command)
    clock.advance(300)
    original_send = clock()
    await drain(service)
    assert service.store.tickets["t1"]["count"] == 0
    clock.advance(60)
    await drain(service)
    item = service.store.tickets["t1"]
    assert item["count"] == 1 and item["last_send"] == original_send
    assert requests[0] == requests[1]  # Retry preserves the UUID and exact content.
    assert service.store.tickets["t1"]["count"] == 1
    assert len(receipts) == 1
    assert "5分钟后将自动关闭" in json.loads(requests[-1]["content"])["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_time", [None, "invalid", "2027-01-15 16:05:00"])
async def test_invalid_send_time_waits_for_raw_receipt_reconciliation(desk, invalid_time):
    service, api, clock = await initialized(desk)
    original_send = api.cli.send_text

    async def incomplete(*args, **kwargs):
        result = await original_send(*args, **kwargs)
        return {**result, "create_time": invalid_time}

    api.cli.send_text = incomplete
    clock.advance(300)
    actual_send = clock()
    await drain(service)
    item = service.store.tickets["t1"]
    assert item["count"] == 0 and item["last_send"] is None
    pending = service.store.pending_sends("t1")[0]
    receipt = api.cli.receipts[pending["uuid"]]
    api.cli.chat_messages.return_value = {
        "items": [
            {
                "message_id": receipt["message_id"],
                "chat_id": "oc_ticket",
                "msg_type": "text",
                "create_time": receipt["create_time"],
                "sender": {"sender_type": "user", "id": pending["actor"]},
                "body": {"content": json.dumps({"text": pending["text"]})},
            }
        ],
        "has_more": False,
    }
    clock.advance(60)
    await service.delivery.reconcile()
    item = service.store.tickets["t1"]
    assert item["count"] == 1 and item["last_send"] == actual_send
    assert not service.store.pending_sends("t1")
    assert len(api.cli.sends) == 2  # Welcome and the original reminder only.


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
    from test_feishu_helpdesk_cli import auth_access_value, success
    from vikingbot.extensions.feishu_helpdesk.cli import LarkCLIAdapter
    from vikingbot.extensions.feishu_helpdesk.config import LarkCLIConfig
    from vikingbot.extensions.feishu_helpdesk.followup import StaleAction

    service, api, clock = await initialized(desk)
    adapter = LarkCLIAdapter(LarkCLIConfig(), "cli_bot")
    adapter._auth_access = AsyncMock(return_value=auth_access_value())
    adapter._command = AsyncMock(return_value=success())
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
    assert adapter._command.await_count == 0


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
@pytest.mark.parametrize("updated_event", [False, True])
async def test_late_ticket_event_binds_chat_without_reverting_scanned_state(
    desk, tmp_path, updated_event
):
    from test_feishu_helpdesk import ticket_event
    from vikingbot.extensions.feishu_helpdesk.channel import FeishuHelpdeskChannel

    service, api, clock = await desk()
    created = clock()
    add_ticket(service, api, clock, chat="")
    clock.advance(60)
    api.tickets["t1"].update(status=2, updated_at=int(clock() * 1000))
    await service.reconcile()
    scanned = copy.deepcopy(service.store.tickets["t1"])
    channel = FeishuHelpdeskChannel(service.config, service.bus)
    channel._followup = service
    channel._helpdesk_api.ensure_in_chat = AsyncMock(return_value=True)
    try:
        await channel._on_ticket(
            ticket_event(
                updated=updated_event,
                ticket_id="t1",
                helpdesk_id="desk",
                created_at=str(int(created * 1000)),
                updated_at=str(int(created * 1000)),
                guest={"id": {"open_id": "ou_stale_guest"}},
            )
        )
        assert channel._ticket_chats["oc_ticket"] == "t1"
        assert service.store.tickets["t1"] == {**scanned, "chat_id": "oc_ticket"}
        # The binding, rather than just the admission cache, must survive restart.
        await service.close()
        restarted, _, _ = await desk(path=tmp_path, clock=clock, api=api)
        await restarted.reconcile()
        await drain(restarted)
        assert len(api.cli.sends) == 1 and api.cli.sends[0][0] == "oc_ticket"
    finally:
        await channel.stop()


@pytest.mark.parametrize("closed", [False, True])
def test_late_chat_binding_rejects_conflicts_and_preserves_ticket_state(tmp_path, closed):
    clock = Clock()
    state = HelpdeskState(tmp_path / "state.sqlite3", clock())
    try:
        ticket = {"ticket_id": "t1", "created_at": int(clock() * 1000), "status": 1}
        state.register(ticket, clock())
        latest = state.register(
            {**ticket, "status": 51 if closed else 2, "updated_at": int((clock() + 60) * 1000)},
            clock(),
        )
        stale = {**ticket, "updated_at": int(clock() * 1000)}
        bound = state.register(stale, clock(), chat_id="oc_ticket")
        assert bound == {**latest, "chat_id": "oc_ticket"}
        with pytest.raises(ValueError, match="Conflicting"):
            state.register(stale, clock(), chat_id="oc_other")
        assert state.tickets["t1"] == bound
        state.save_outbox(
            "t1:welcome",
            {
                "ticket": "t1",
                "chat_id": "oc_ticket",
                "uuid": "old-uuid",
                "attempted_at": clock(),
                "actor": "ou_operator",
            },
        )
        corrected = state.register(
            {
                **ticket,
                "status": latest["status"],
                "updated_at": int(latest["updated"] * 1000),
            },
            clock(),
            chat_id="oc_authoritative",
            authoritative=True,
        )
        assert corrected["chat_id"] == "oc_authoritative"
        rebased = state.outbox("t1:welcome")
        assert rebased["chat_id"] == "oc_authoritative"
        assert rebased["uuid"] != "old-uuid"
        assert rebased["attempted_at"] is None
        assert rebased["actor"] is None
    finally:
        state.close()


def test_ticket_generation_ignores_volatile_profile_fields(tmp_path):
    clock = Clock()
    state = HelpdeskState(tmp_path / "state.sqlite3", clock())
    try:
        ticket = {
            "ticket_id": "t1",
            "created_at": int(clock() * 1000),
            "status": 2,
            "guest": {"id": "ou_guest", "avatar_url": "https://s1.example/avatar"},
            "agents": [{"id": "ou_agent", "avatar_url": "https://s1.example/agent"}],
        }
        first = state.register(ticket, clock())
        second = state.register(
            {
                **ticket,
                "guest": {**ticket["guest"], "avatar_url": "https://s2.example/avatar"},
                "agents": [
                    {**ticket["agents"][0], "avatar_url": "https://s3.example/agent"}
                ],
            },
            clock(),
        )
        assert second["generation"] == first["generation"]

        changed = state.register(
            {
                **ticket,
                "agents": [*ticket["agents"], {"id": "ou_second_agent"}],
            },
            clock(),
        )
        assert changed["generation"] == first["generation"] + 1
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
    assert call["max_tokens"] == 1024
    assert call["reasoning_effort"] == "none"
    from openviking.utils.token_estimation import estimate_serialized_tokens

    assert estimate_serialized_tokens(call["messages"]) <= 3000


@pytest.mark.asyncio
async def test_judge_summarizes_ticket_problem_without_copying_raw_messages():
    provider = SimpleNamespace(
        chat=AsyncMock(
            return_value=LLMResponse(
                content=json.dumps(
                    {
                        "summary": "齐色样单无法上传，业务方已提供截图，等待开发排查上传失败原因。"
                    },
                    ensure_ascii=False,
                )
            )
        )
    )
    judge = ConversationJudge(provider, "model")

    summary = await judge.summarize(
        {"id": "t1", "guest": {"id": "ou_guest"}, "agents": []},
        [
            {
                "im_id": "om_welcome",
                "human": False,
                "sender": "",
                "text": "你好，请描述遇到的问题。",
                "time": 1,
            },
            {
                "im_id": "om_problem",
                "human": True,
                "sender": "ou_guest",
                "text": "你好，麻烦帮我看下这两个款的齐色单上传不了",
                "time": 2,
            },
            {
                "im_id": "om_image",
                "human": True,
                "sender": "ou_guest",
                "text": "[image]",
                "image_summary": "上传页面提示文件解析失败",
                "time": 3,
            },
        ],
    )

    assert summary == "齐色样单无法上传，业务方已提供截图，等待开发排查上传失败原因。"
    call = provider.chat.call_args.kwargs
    assert call["max_tokens"] == 256
    assert call["session_id"] == "helpdesk-summary:t1"
    assert call["tools"] == [] and call["thinking"] is False
    payload = call["messages"][1]["content"]
    assert "齐色单上传不了" in payload
    assert "上传页面提示文件解析失败" in payload
    assert "请描述遇到的问题" not in payload


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
async def test_empty_discovery_accepts_null_ticket_list(desk):
    service, api, clock = await desk()
    clock.advance(120)
    api.tickets_page.return_value = {"tickets": None, "total": 0}

    await service.sync.discover()

    assert service.store.meta("create_cursor") == clock()
    assert service.store.meta("update_cursor") == clock()


@pytest.mark.asyncio
async def test_discovery_returns_newly_registered_tickets(desk):
    service, api, clock = await desk()
    add_ticket(service, api, clock, "existing")
    clock.advance(1)
    api.tickets["new"] = {
        "ticket_id": "new",
        "helpdesk_id": "desk",
        "status": 2,
        "created_at": int(clock() * 1000),
        "updated_at": int(clock() * 1000),
        "guest": {"id": "ou_new"},
        "agents": [{"id": "ou_agent"}],
        "collaborators": [],
    }

    assert await service.sync.discover() == ["new"]
    assert "existing" in service.store.tickets
    assert "new" in service.store.tickets


@pytest.mark.asyncio
async def test_discovery_and_reconcile_include_changed_existing_tickets(desk):
    service, api, clock = await desk()
    add_ticket(service, api, clock, "existing", chat="")
    clock.advance(1)
    api.tickets["existing"].update(
        chat_id="oc_real_group",
        updated_at=int(clock() * 1000),
    )

    await service.reconcile(max_existing=0)

    assert api.ticket.await_args_list[0].args[0] == "existing"
    assert service.store.tickets["existing"]["chat_id"] == "oc_real_group"
    assert await service.sync.discover() == []

    clock.advance(1)
    api.tickets["existing"].update(status=51, updated_at=int(clock() * 1000))
    assert await service.sync.discover() == ["existing"]


@pytest.mark.asyncio
async def test_failed_sync_is_requeued_with_bounded_backoff(desk):
    service, api, clock = await desk()
    add_ticket(service, api, clock)
    service.sync.ticket = AsyncMock(side_effect=[RuntimeError("temporary"), None])

    await service.reconcile(discover=False, ids=["t1"], max_existing=0)

    assert service._sync_failures["t1"] == 1
    assert service._sync_retry_at["t1"] == clock() + 10
    assert "t1" in service._dirty

    await service.reconcile(discover=False, ids=["t1"], max_existing=0)
    assert service.sync.ticket.await_count == 1

    clock.advance(10)
    await service.reconcile(discover=False, ids=["t1"], max_existing=0)
    assert service.sync.ticket.await_count == 2
    assert "t1" not in service._sync_retry_at
    assert "t1" not in service._sync_failures


@pytest.mark.asyncio
async def test_reconcile_prioritizes_new_tickets_and_sweeps_a_bounded_batch(desk):
    service, api, clock = await desk()
    for index in range(5):
        add_ticket(service, api, clock, f"t{index}", chat=f"oc_t{index}")
    clock.advance(1)
    api.tickets["new"] = {
        "ticket_id": "new",
        "helpdesk_id": "desk",
        "status": 2,
        "created_at": int(clock() * 1000),
        "updated_at": int(clock() * 1000),
        "guest": {"id": "ou_new"},
        "agents": [{"id": "ou_agent"}],
        "collaborators": [],
    }

    await service.reconcile(max_existing=2)
    first_calls = [call.args[0] for call in api.ticket.await_args_list]
    assert first_calls == ["new", "t0", "t1"]

    api.ticket.reset_mock()
    await service.reconcile(max_existing=2)
    second_calls = [call.args[0] for call in api.ticket.await_args_list]
    assert second_calls == ["t2", "t3"]


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
        receipt = await original_send(chat, text, key, before_call=before_call)
        human(service, clock, "om_auto1", sender="ou_operator", text=text)
        human(service, clock, "om_manual", sender="ou_operator", text="我来处理")
        return receipt

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
        receipt = await original_send(chat, text, key, before_call=before_call)
        human(service, clock, "om_auto2", sender="ou_operator", text=text)
        return receipt

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
    assert api.cli.sends[-1][1] == service.config.helpdesk.messages.render(
        "问题已解决，工单已关闭。"
    )


@pytest.mark.asyncio
async def test_close_preparation_failure_never_persists_a_pending_put(desk):
    service, api, clock = await initialized(desk)
    service.judge.judge.return_value = Decision("resolved", reason="业务确认恢复")
    human(service, clock)

    async def unavailable(*args, **kwargs):
        assert service.store.tickets["t1"]["close_pending"] is None
        raise LarkCLIError("credential format unsupported", rejected=True)

    api.cli.close_ticket = unavailable
    await drain(service)
    assert service.store.tickets["t1"]["close_pending"] is None
    assert api.tickets["t1"]["status"] == 2 and len(api.cli.sends) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [154003, 99991668, 99991672, 99991679])
async def test_explicit_close_refusal_clears_only_this_pending_attempt_and_can_retry(desk, code):
    service, api, clock = await initialized(desk)
    service.judge.judge.return_value = Decision("resolved", reason="业务确认恢复")
    human(service, clock)
    generation = service.store.tickets["t1"]["generation"]
    original = api.cli.close_ticket

    async def refused(ticket, authorization, body, *, before_call):
        assert service.store.tickets["t1"]["close_pending"] is None
        await before_call("ou_operator")
        assert service.store.tickets["t1"]["close_pending"]
        raise LarkCLIError("close API refused the request", code=code, rejected=True)

    api.cli.close_ticket = refused
    await drain(service)
    assert service.store.tickets["t1"]["close_pending"] is None

    async def permitted(*args, **kwargs):
        assert service.store.tickets["t1"]["generation"] == generation
        return await original(*args, **kwargs)

    api.cli.close_ticket = permitted
    clock.advance(60)
    await drain(service)
    assert len(api.cli.closes) == 1 and api.tickets["t1"]["status"] == 51


@pytest.mark.asyncio
async def test_uncertain_open_close_is_reconciled_without_replaying_put(desk, tmp_path):
    service, api, clock = await initialized(desk, path=tmp_path)
    service.judge.judge.return_value = Decision("resolved", reason="业务确认恢复")
    human(service, clock)
    attempts = []

    async def uncertain(ticket, authorization, body, *, before_call):
        await before_call("ou_operator")
        attempts.append(copy.deepcopy(body))
        raise LarkCLIError("response lost", retryable=True)

    api.cli.close_ticket = uncertain
    await drain(service)
    assert service.store.tickets["t1"]["close_pending"]
    for _ in range(3):
        clock.advance(60)
        await service.reconcile()
        await drain(service)
    assert len(attempts) == 1 and len(api.cli.sends) == 1
    # The submission fence survives restart, including while details say OPEN.
    await service.close()
    service, _, _ = await desk(path=tmp_path, api=api, clock=clock)
    clock.advance(60)
    await service.reconcile()
    await drain(service)
    assert len(attempts) == 1
    # Delayed completion may be observed after several OPEN detail responses.
    api.tickets["t1"].update(
        status=51,
        solve=2,
        comments=[{"content": attempts[0]["comment"]}],
        updated_at=int(clock() * 1000),
    )
    clock.advance(60)
    await service.reconcile()
    await drain(service)
    assert len(attempts) == 1 and len(api.cli.sends) == 2
    assert service.store.tickets["t1"]["close_pending"] is None
    await drain(service)
    assert len(api.cli.sends) == 2


@pytest.mark.asyncio
async def test_closed_ticket_notice_failure_is_dead_lettered_without_retry(desk):
    service, api, clock = await desk()
    item = add_ticket(service, api, clock)
    service.store.save(
        item,
        status=51,
        closed_solved=1,
        notice_pending=True,
        retry_at=clock() + 60,
    )
    api.cli.fail_send = True

    await service._notice(service.store.tickets["t1"])

    current = service.store.tickets["t1"]
    out = service.store.outbox("t1:notice")
    assert current["status"] == 51
    assert current["notice_pending"] is False
    assert current["retry_at"] == 0
    assert out["attempted_at"] is not None
    assert out["dead_letter"] is True
    assert not service._due(current)


@pytest.mark.asyncio
async def test_startup_retires_previously_attempted_closed_notice(desk):
    service, api, clock = await desk()
    item = add_ticket(service, api, clock)
    service.store.save(
        item,
        status=51,
        closed_solved=1,
        notice_pending=True,
        retry_at=clock() + 60,
    )
    service.store.save_outbox(
        "t1:notice",
        {
            "ticket": "t1",
            "kind": "notice",
            "attempted_at": clock() - 1,
            "text": "closed",
        },
    )

    service._retire_attempted_closed_notices()

    current = service.store.tickets["t1"]
    out = service.store.outbox("t1:notice")
    assert current["notice_pending"] is False
    assert current["retry_at"] == 0
    assert out["dead_letter"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("after_guard", [False, True])
async def test_close_cancellation_only_keeps_pending_after_final_guard(desk, after_guard):
    service, api, clock = await initialized(desk)

    async def cancelled(*args, before_call, **kwargs):
        if after_guard:
            await before_call("ou_operator")
        raise asyncio.CancelledError()

    api.cli.close_ticket = cancelled
    with pytest.raises(asyncio.CancelledError):
        await service._close(service.store.tickets["t1"], solved=2, reason="业务确认恢复")
    assert bool(service.store.tickets["t1"]["close_pending"]) is after_guard


@pytest.mark.asyncio
async def test_timeout_close_rechecks_wall_clock_after_authorization_preparation(desk):
    service, api, clock = await initialized(desk)
    service.judge.judge.return_value = Decision("awaiting_details", "请补充问题。")
    clock.advance(300)
    await drain(service)
    clock.advance(300)

    async def clock_changed():
        clock.advance(-60)

    api.cli.before_close = clock_changed
    await drain(service)
    assert not api.cli.closes
    assert service.store.tickets["t1"]["close_pending"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("during_read", [False, True])
async def test_recovery_drops_old_close_notice_when_ticket_reopens(desk, during_read):
    service, api, clock = await initialized(desk)
    service.judge.judge.return_value = Decision("resolved", reason="业务确认恢复")
    human(service, clock)
    close = api.cli.close_ticket

    async def lost(*args, **kwargs):
        await close(*args, **kwargs)
        raise LarkCLIError("response lost", retryable=True)

    api.cli.close_ticket = lost
    await drain(service)
    assert service.store.tickets["t1"]["close_pending"]
    clock.advance(60)
    reads = 0

    async def reopen(tid):
        nonlocal reads
        reads += 1
        old_response = copy.deepcopy(api.tickets[tid])
        if reads == 2:
            # _execute's first sync saw CLOSED. The recovery read is now stale
            # or itself returns the newer OPEN state with the old close comment.
            api.tickets[tid].update(status=2, updated_at=int(clock() * 1000))
            if during_read:
                service.register(copy.deepcopy(api.tickets[tid]), authoritative=True)
                return old_response
        return copy.deepcopy(api.tickets[tid])

    api.ticket.side_effect = reopen
    await drain(service)
    item = service.store.tickets["t1"]
    assert item["status"] == 2 and item["close_pending"] is None
    assert item["close_cycle"] == 1 and item["reopen_waiting_for_human"]
    assert not item["notice_pending"] and len(api.cli.sends) == 1
    assert service.store.outbox("t1:notice") is None
    # Once the new round really resolves, it gets its own notice and UUID.
    api.cli.close_ticket = close
    clock.advance(1)
    human(service, clock, "om_new_resolution")
    await drain(service)
    assert len(api.cli.closes) == 2 and len(api.cli.sends) == 2
    assert service.store.outbox("t1:notice:1")["message_id"]


@pytest.mark.asyncio
async def test_close_confirmation_cannot_overwrite_reopened_state(desk):
    service, api, clock = await initialized(desk)
    service.judge.judge.return_value = Decision("resolved", reason="业务确认恢复")
    human(service, clock)
    close = api.cli.close_ticket

    async def reopened(*args, **kwargs):
        await close(*args, **kwargs)
        closed_response = copy.deepcopy(api.tickets["t1"])
        service.register(closed_response, authoritative=True)
        clock.advance(1)
        api.tickets["t1"].update(status=2, updated_at=int(clock() * 1000))
        service.register(copy.deepcopy(api.tickets["t1"]), authoritative=True)
        api.ticket.side_effect = None
        api.ticket.return_value = closed_response
        return {}

    api.cli.close_ticket = reopened
    await drain(service)
    item = service.store.tickets["t1"]
    assert item["status"] == 2 and item["close_cycle"] == 1
    assert not item["notice_pending"] and len(api.cli.sends) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["poll", "human"])
async def test_concurrent_close_confirmation_recovers_notice_without_closing_twice(desk, change):
    service, api, clock = await initialized(desk)
    service.judge.judge.return_value = Decision("resolved", reason="业务确认恢复")
    human(service, clock)
    close = api.cli.close_ticket

    async def changed(*args, **kwargs):
        await close(*args, **kwargs)
        if change == "poll":
            service.register(copy.deepcopy(api.tickets["t1"]), authoritative=True)
        else:
            human(service, clock, "om_during_confirmation")
        return {}

    api.cli.close_ticket = changed
    await drain(service)
    item = service.store.tickets["t1"]
    assert item["status"] == 51 and item["close_pending"] is None
    assert not item["notice_pending"]
    assert len(api.cli.closes) == 1 and len(api.cli.sends) == 2
    assert service.store.outbox("t1:notice")["settled"]


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
        channel._helpdesk_api.ticket = AsyncMock(
            return_value={**api.tickets["t1"], "chat_id": "oc_unrelated"}
        )
        event.event.chat_id = "oc_unrelated"
        await channel._on_ticket_message(event)
        assert service.store.tickets["t1"]["chat_id"] == "oc_unrelated"
        event.event.chat_id = "oc_wrong"
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


@pytest.mark.asyncio
async def test_web_numeric_receipt_links_later_im_echo(desk):
    service, api, clock = await desk()
    add_ticket(service, api, clock)
    key = "t1:welcome"
    service.store.save_outbox(
        key,
        {
            "ticket": "t1",
            "chat_id": "oc_ticket",
            "uuid": "stable",
            "text": "【AI分身自动回复】\n你好",
            "kind": "welcome",
            "generation": 1,
            "attempted_at": clock.now,
            "actor": "ou_operator",
            "message_id": "7704209428689505542",
            "transport": "feishu_web",
            "cycle": 0,
        },
    )
    assert service.delivery.observe(
        "t1",
        {
            "im_id": "om_echo",
            "sender": "ou_service_desk",
            "text": "【AI分身自动回复】\n你好",
            "time": clock.now + 1,
        },
    )
    receipt = service.store.outbox(key)
    assert receipt["message_id"] == "7704209428689505542"
    assert receipt["im_message_id"] == "om_echo"
    assert service.store.automatic("om_echo")


@pytest.mark.asyncio
async def test_followup_pauses_when_actor_is_not_a_ticket_agent(desk):
    service, api, clock = await desk()
    item = add_ticket(service, api, clock)
    service.reply_sender = SimpleNamespace(can_send=lambda ticket: False)
    with pytest.raises(StaleAction):
        await service._send(item, "welcome", "hello")
    current = service.store.tickets["t1"]
    assert current["paused"] is True
    assert current["pause_reason"] == "actor_not_assigned"
    assert service.store.outbox("t1:welcome") is None


@pytest.mark.asyncio
async def test_numeric_helpdesk_messages_get_provenance_from_ticket_participants(desk):
    service, api, clock = await desk()
    add_ticket(service, api, clock)
    api.messages["t1"] = [
        {
            "id": "tm_guest",
            "message_id": "7704222585776524536",
            "created_at": int(clock() * 1000),
            "content": "供应链管理人工客服（SCM）",
            "user_id": "ou_guest",
            "message_type": "text",
        },
        {
            "id": "tm_system",
            "message_id": "7704222618425904137",
            "created_at": int(clock() * 1000),
            "content": "已分配人工",
            "user_id": "",
            "message_type": "text",
        },
    ]
    await service.reconcile()
    messages = {m["ticket_id"]: m for m in service.store.messages("t1")}
    assert messages["tm_guest"]["human"] is True
    assert messages["tm_system"]["human"] is False


@pytest.mark.asyncio
async def test_start_repairs_legacy_numeric_message_provenance(desk):
    service, api, clock = await desk()
    add_ticket(service, api, clock)
    service.store.observe(
        "t1",
        {
            "ticket_id": "tm_guest",
            "im_id": "7704222585776524536",
            "sender": "ou_guest",
            "human": None,
            "time": clock() - 60,
            "text": "供应链管理人工客服（SCM）",
            "kind": "text",
        },
    )
    service.store.observe(
        "t1",
        {
            "ticket_id": "tm_system",
            "im_id": "7704222618425904137",
            "sender": "",
            "human": None,
            "time": clock() - 59,
            "text": "已分配人工",
            "kind": "text",
        },
    )
    service._reclassify_numeric_messages()
    messages = {m["ticket_id"]: m for m in service.store.messages("t1")}
    assert messages["tm_guest"]["human"] is True
    assert messages["tm_system"]["human"] is False
    assert service.store.tickets["t1"]["last_human"] == clock()


@pytest.mark.asyncio
async def test_judge_accepts_json_inside_model_prose_and_fences():
    provider = SimpleNamespace(
        chat=AsyncMock(
            return_value=LLMResponse(
                content=(
                    "分析如下，仅返回 JSON：\n```json\n"
                    + json.dumps(
                        {
                            "scenario": "awaiting_details",
                            "evidence_ids": ["om_1"],
                            "reason": "客户只选择了分类",
                            "followup": "你好，请补充具体问题。",
                        },
                        ensure_ascii=False,
                    )
                    + "\n```\n以上。"
                )
            )
        )
    )
    judge = ConversationJudge(provider, "model")
    result = await judge.judge(
        {"id": "t1", "guest": {"id": "ou_guest"}},
        [
            {
                "im_id": "om_1",
                "human": True,
                "sender": "ou_guest",
                "text": "供应链管理人工客服（SCM）",
                "time": 1,
            }
        ],
    )
    assert result.scenario == "awaiting_details"
    assert result.followup == "您好，请补充具体问题。"


@pytest.mark.asyncio
async def test_judge_coalesces_ticket_and_im_echo_before_evidence_validation():
    provider = SimpleNamespace(
        chat=AsyncMock(
            return_value=LLMResponse(
                content=json.dumps(
                    {
                        "scenario": "awaiting_confirmation",
                        "evidence_ids": ["om_answer", "om_exception"],
                        "reason": "处理方补充例外后等待业务确认",
                        "followup": "请确认是否已解答。",
                    },
                    ensure_ascii=False,
                )
            )
        )
    )
    judge = ConversationJudge(provider, "model")
    result = await judge.judge(
        {"id": "t1", "guest": {"id": "ou_guest"}, "agents": []},
        [
            {
                "ticket_id": "tm_question",
                "human": True,
                "sender": "ou_guest",
                "text": "问题是什么？",
                "time": 1,
            },
            {
                "ticket_id": "tm_answer",
                "human": True,
                "sender": "ou_relay",
                "text": "这是答案。",
                "time": 2,
            },
            {
                "im_id": "om_answer",
                "human": True,
                "sender": "ou_different_relay",
                "text": "这是答案。",
                "time": 2,
            },
            {
                "ticket_id": "tm_exception",
                "human": True,
                "sender": "ou_relay",
                "text": "但还有一个例外。",
                "time": 3,
            },
            {
                "im_id": "om_exception",
                "human": True,
                "sender": "ou_different_relay",
                "text": "但还有一个例外。",
                "time": 3,
            },
        ],
    )
    assert result.scenario == "awaiting_confirmation"
    prompt_messages = json.loads(provider.chat.await_args.kwargs["messages"][1]["content"])
    assert [item["id"] for item in prompt_messages] == [
        "tm_question",
        "om_answer",
        "om_exception",
    ]


@pytest.mark.asyncio
async def test_judge_trusts_human_relay_sender_as_processing_side():
    provider = SimpleNamespace(
        chat=AsyncMock(
            return_value=LLMResponse(
                content=json.dumps(
                    {
                        "scenario": "awaiting_confirmation",
                        "evidence_ids": ["om_answer", "om_thanks", "om_exception"],
                        "reason": "处理方给出结论后又补充例外，等待业务确认",
                        "followup": "请确认上述说明是否已解答您的问题。",
                    },
                    ensure_ascii=False,
                )
            )
        )
    )
    judge = ConversationJudge(provider, "model")
    result = await judge.judge(
        {"id": "t1", "guest": {"id": "ou_guest"}, "agents": [{"id": "ou_named_agent"}]},
        [
            {
                "im_id": "om_question",
                "human": True,
                "sender": "ou_guest",
                "text": "企划变更后的推送范围是什么？",
                "time": 1,
            },
            {
                "im_id": "om_answer",
                "human": True,
                "sender": "ou_relay_agent",
                "text": "只会推送给当前企划和当前设计师。",
                "time": 2,
            },
            {
                "im_id": "om_thanks",
                "human": True,
                "sender": "ou_guest",
                "text": "好的，谢谢",
                "time": 3,
            },
            {
                "im_id": "om_exception",
                "human": True,
                "sender": "ou_relay_agent",
                "text": "但预报价评审的范围会更大。",
                "time": 4,
            },
        ],
    )
    assert result.scenario == "awaiting_confirmation"
    assert result.followup == "请确认上述说明是否已解答您的问题。"


@pytest.mark.asyncio
async def test_judge_receives_combined_multi_image_summary():
    provider = SimpleNamespace(
        chat=AsyncMock(
            return_value=LLMResponse(
                content=json.dumps(
                    {
                        "scenario": "awaiting_handler",
                        "evidence_ids": ["om_images"],
                        "reason": "业务已提供两张问题截图",
                        "followup": "请处理方排查批量下载失败原因。",
                    },
                    ensure_ascii=False,
                )
            )
        )
    )
    judge = ConversationJudge(provider, "vision-model")
    await judge.judge(
        {"id": "t1", "guest": {"id": "ou_guest"}, "agents": []},
        [
            {
                "im_id": "om_images",
                "human": True,
                "sender": "ou_guest",
                "kind": "post",
                "text": "批量下载失败",
                "image_summary": "图1：失败任务列表；图2：下载超时提示",
                "time": 1,
            }
        ],
    )

    prompt_messages = json.loads(provider.chat.await_args.kwargs["messages"][1]["content"])
    assert prompt_messages == [
        {
            "id": "om_images",
            "role": "guest",
            "kind": "post",
            "text": "批量下载失败",
            "image_summary": "图1：失败任务列表；图2：下载超时提示",
        }
    ]


@pytest.mark.asyncio
async def test_judge_accepts_awaiting_details_despite_unrelated_latest_chatter():
    provider = SimpleNamespace(
        chat=AsyncMock(
            return_value=LLMResponse(
                content=json.dumps(
                    {
                        "scenario": "awaiting_details",
                        "evidence_ids": ["om_guest"],
                        "reason": "业务仅选择了服务分类，尚未描述具体问题",
                        "followup": "请补充具体遇到的问题。",
                    },
                    ensure_ascii=False,
                )
            )
        )
    )
    judge = ConversationJudge(provider, "model")
    result = await judge.judge(
        {"id": "t1", "guest": {"id": "ou_guest"}, "agents": [{"id": "ou_agent"}]},
        [
            {
                "im_id": "om_guest",
                "human": True,
                "sender": "ou_guest",
                "text": "供应链管理人工客服（SCM）",
                "time": 1,
            },
            {
                "im_id": "om_chatter",
                "human": True,
                "sender": "ou_agent",
                "text": "我已经重启 拉闸",
                "time": 2,
            },
        ],
    )
    assert result.scenario == "awaiting_details"
    assert result.followup == "请补充具体遇到的问题。"


@pytest.mark.asyncio
async def test_dead_letter_stops_receipt_reconciliation(desk):
    service, api, clock = await desk()
    add_ticket(service, api, clock)
    service.store.save(
        service.store.tickets["t1"], paused=True, pause_reason="receipt_expired"
    )
    service.store.save_outbox(
        "t1:welcome",
        {
            "ticket": "t1",
            "chat_id": "oc_ticket",
            "kind": "welcome",
            "attempted_at": clock() - 4000,
            "actor": "ou_operator",
            "text": "hello",
        },
    )
    await service.delivery.reconcile()
    assert service.store.outbox("t1:welcome")["dead_letter"] is True
    assert service.store.pending_sends("t1") == []
    api.cli.chat_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_failure_does_not_block_due_first_reminder(desk):
    service, api, clock = await initialized(desk)
    service.judge.judge = AsyncMock(
        side_effect=[
            Decision("resolved", reason="业务确认恢复"),
            Decision("awaiting_details", "请补充问题。"),
        ]
    )
    human(service, clock)
    clock.advance(300)

    async def refused(*args, **kwargs):
        raise LarkCLIError("close refused", rejected=True)

    api.cli.close_ticket = refused
    await drain(service)
    assert len(api.cli.sends) == 1
    assert service.store.tickets["t1"]["retry_at"] == clock() + 5

    clock.advance(5)
    await drain(service)
    assert len(api.cli.sends) == 2
    assert "请补充问题" in api.cli.sends[-1][1]
