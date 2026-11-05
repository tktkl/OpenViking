"""Escalation routing, timing, and self-healing behavior."""

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from vikingbot.config.schema import FeishuChannelConfig
from vikingbot.extensions.feishu_helpdesk.cli import LarkCLIError
from vikingbot.extensions.feishu_helpdesk.decision import Decision
from vikingbot.extensions.feishu_helpdesk.escalation import (
    detect_skill_group,
    detect_system,
    normalize_system,
    render_escalation,
    ticket_subject,
)

from test_helpdesk_followup import add_ticket, desk as desk, drain, human, initialized

TMS_GROUP = "oc_9d44f08fa000144519d54ed5565730f1"
COMMON_GROUP = "oc_c3deaf22ddfa568d745fd4d881e2ddde"
ESCALATION = {
    "enabled": True,
    "interval_minutes": 30,
    "groups": {
        "tms": TMS_GROUP,
        "scm": COMMON_GROUP,
        "srm": COMMON_GROUP,
        "qms": COMMON_GROUP,
        "pdm": COMMON_GROUP,
        "plm": COMMON_GROUP,
    },
}

SCOPE_ESCALATION = {
    **ESCALATION,
    "followup_skill_groups": {
        "TMS技能组": ["TMS"],
        "PDM技能组": ["PDM"],
        "PDM英文技能组": ["PDM"],
        "QMS/SCM英文版技能组": ["SCM", "QMS"],
        "供应链业务线": ["SCM", "QMS", "SRM", "PDM", "PLM"],
        "紧急故障（大供应链）": ["SCM", "QMS", "SRM", "PDM", "PLM"],
    },
}


def test_escalation_defaults_and_group_keys_are_normalized():
    config = FeishuChannelConfig().helpdesk
    assert config.followup_interval_minutes == 5
    assert config.escalation.interval_minutes == 30
    assert config.escalation.groups == {}

    configured = FeishuChannelConfig(
        helpdesk={
            "escalation": {
                "interval_minutes": 30,
                "groups": {"pdm": COMMON_GROUP, "TMS": TMS_GROUP},
            }
        }
    ).helpdesk.escalation
    assert configured.groups == {"PDM": COMMON_GROUP, "TMS": TMS_GROUP}


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "10", None])
def test_escalation_interval_is_a_strict_positive_integer(value):
    with pytest.raises(ValidationError):
        FeishuChannelConfig(helpdesk={"escalation": {"interval_minutes": value}})


@pytest.mark.parametrize("field", ["groups", "pdm"])
def test_escalation_rejects_invalid_group_targets(field):
    groups = {"PDM": COMMON_GROUP, "TMS": "not-a-chat"} if field == "groups" else {"": COMMON_GROUP}
    with pytest.raises(ValidationError):
        FeishuChannelConfig(helpdesk={"escalation": {"groups": groups}})


def test_skill_group_scope_reuses_escalation_groups():
    configured = FeishuChannelConfig(
        helpdesk={"escalation": SCOPE_ESCALATION}
    ).helpdesk.escalation
    assert configured.followup_skill_groups["供应链业务线"] == [
        "SCM",
        "QMS",
        "SRM",
        "PDM",
        "PLM",
    ]
    assert (
        detect_skill_group(
            {"actual_agent_group": "供应链业务线"},
            list(configured.followup_skill_groups),
        )
        == "供应链业务线"
    )
    assert (
        detect_skill_group(
            {"hit_agent_groups": [{"name": "紧急故障（大供应链）"}]},
            list(configured.followup_skill_groups),
        )
        == "紧急故障（大供应链）"
    )


def test_skill_group_scope_rejects_unconfigured_system():
    with pytest.raises(ValidationError):
        FeishuChannelConfig(
            helpdesk={
                "escalation": {
                    **ESCALATION,
                    "followup_skill_groups": {"供应链业务线": ["WMS"]},
                }
            }
        )


def test_system_detection_uses_initial_human_choice_not_bot_option_list():
    messages = [
        {"human": False, "text": "产品信息人工客服（PDM） 物流管理人工客服（TMS）"},
        {"human": True, "text": "人工"},
        {"human": True, "text": "供应链管理人工客服（SCM）"},
        {"human": True, "text": "后来提到 TMS"},
    ]
    assert detect_system({}, messages, ["TMS", "SCM", "PDM"]) == "SCM"


def test_system_detection_falls_back_to_assignment_metadata():
    ticket = {"actual_agent_group": "产品信息技能组（PDM）"}
    assert detect_system(ticket, [], ["TMS", "PDM"]) == "PDM"
    assert detect_system({}, [], ["TMS", "PDM"]) == ""
    assert normalize_system(" pdm ") == "PDM"
    assert normalize_system("p_d_m") == "PDM"
    assert normalize_system("hr_system") == "HR_SYSTEM"


def test_escalation_text_uses_customer_name_and_clickable_ticket_group():
    text = render_escalation(
        "7704211350305655987",
        "TMS",
        "oc_d61a2638665e64f5a98e2fb5c401746d",
        10,
        guest={"name": "Darry"},
    )
    assert ticket_subject({"name": "Darry"}, "TMS") == "Darry 的 TMS 工单"
    assert "Darry 的 TMS 工单（7704211350305655987）" in text
    assert (
        "https://applink.feishu.cn/client/chat/open?openChatId="
        "oc_d61a2638665e64f5a98e2fb5c401746d"
    ) in text
    assert "原工单群：oc_d61a2638665e64f5a98e2fb5c401746d" not in text
    assert ticket_subject({}, "TMS") == "客户 的 TMS 工单"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "system,expected",
    [
        ("TMS", TMS_GROUP),
        ("SCM", COMMON_GROUP),
        ("SRM", COMMON_GROUP),
        ("QMS", COMMON_GROUP),
        ("PDM", COMMON_GROUP),
        ("PLM", COMMON_GROUP),
    ],
)
async def test_system_to_group_routing(desk, system, expected):
    service, _, _ = await desk(minutes=5, escalation=ESCALATION)
    assert service._escalation_target(system) == expected


@pytest.mark.asyncio
async def test_followup_scope_pauses_outside_group_and_resumes_when_switched_back(desk):
    service, api, clock = await desk(minutes=5, escalation=SCOPE_ESCALATION)
    add_ticket(service, api, clock)
    api.tickets["t1"]["actual_agent_group"] = "TMS技能组"
    service.register(api.tickets["t1"], authoritative=True)
    item = service.store.tickets["t1"]
    assert item["followup_skill_group"] == "TMS技能组"
    assert item["followup_systems"] == ["TMS"]
    assert item["paused"] is False

    service.on_leave_chat = AsyncMock(return_value=True)
    clock.advance(1)
    api.tickets["t1"].update(
        actual_agent_group="财务技能组",
        hit_agent_groups=[{"name": "财务技能组"}],
        updated_at=int(clock() * 1000),
    )
    service.register(api.tickets["t1"], authoritative=True)
    item = service.store.tickets["t1"]
    assert item["paused"] is True
    assert item["pause_reason"] == "outside_followup_scope"
    assert item["scope_exit_pending"] is True

    await service.reconcile(ids=["t1"])
    await drain(service)
    service.on_leave_chat.assert_awaited_once_with("oc_ticket")
    item = service.store.tickets["t1"]
    assert item["scope_exit_pending"] is False
    assert item["scope_exit_attempted"] is True
    assert not api.cli.sends

    clock.advance(1)
    api.tickets["t1"].update(
        actual_agent_group="供应链业务线",
        hit_agent_groups=[{"name": "供应链业务线"}],
        updated_at=int(clock() * 1000),
    )
    service.register(api.tickets["t1"], authoritative=True)
    item = service.store.tickets["t1"]
    assert item["paused"] is False
    assert item["pause_reason"] == ""
    assert item["followup_systems"] == ["SCM", "QMS", "SRM", "PDM", "PLM"]
    assert item["escalation_chat_id"] == COMMON_GROUP
    assert item["scope_exit_attempted"] is False


@pytest.mark.asyncio
async def test_three_minute_reminders_skip_welcome_outside_followup_scope(desk):
    service, api, clock = await desk(minutes=5, escalation=SCOPE_ESCALATION)
    add_ticket(service, api, clock)
    api.tickets["t1"]["actual_agent_group"] = "财务技能组"
    service.register(api.tickets["t1"], authoritative=True)
    service.on_leave_chat = AsyncMock(return_value=True)
    await service.reconcile(ids=["t1"])
    await drain(service)
    assert not api.cli.sends
    assert service.store.tickets["t1"]["pause_reason"] == "outside_followup_scope"


@pytest.mark.asyncio
async def test_one_five_minute_reminder_then_thirty_minute_escalation(desk):
    service, api, clock = await initialized(desk, minutes=5, escalation=ESCALATION)
    service.judge.judge.return_value = Decision("awaiting_handler", "请研发继续排查。")
    human(service, clock, text="产品信息人工客服（PDM）")
    await drain(service)

    clock.advance(300)
    await drain(service)
    item = service.store.tickets["t1"]
    assert item["count"] == 1 and item["escalation_started_at"] is None
    assert "5分钟后将升级处理" in api.cli.sends[-1][1]

    clock.advance(299)
    await drain(service)
    assert item["escalation_started_at"] is None
    clock.advance(1)
    await drain(service)
    item = service.store.tickets["t1"]
    assert item["escalation_system"] == "PDM"
    assert item["escalation_chat_id"] == COMMON_GROUP
    assert item["escalation_started_at"] == clock()
    assert item["escalation_last_sent"] == clock()
    assert item["escalation_count"] == 1
    assert api.cli.sends[-1][0] == COMMON_GROUP
    assert service.store.outbox("t1:escalation:1")["transport"] == "feishu_bot"
    assert "客户 的 PDM 工单（t1）" in api.cli.sends[-1][1]
    assert "openChatId=oc_ticket" in api.cli.sends[-1][1]
    assert "本群每 30 分钟提醒一次" in api.cli.sends[-1][1]
    ticket_sends = [send for send in api.cli.sends if send[0] == "oc_ticket"]
    assert len(ticket_sends) == 2

    clock.advance(1799)
    await drain(service)
    assert service.store.tickets["t1"]["escalation_count"] == 1
    clock.advance(1)
    await drain(service)
    item = service.store.tickets["t1"]
    assert item["escalation_count"] == 2
    assert api.cli.sends[-1][0] == COMMON_GROUP
    assert not api.cli.closes


@pytest.mark.asyncio
async def test_ticket_group_stays_silent_but_escalation_continues_after_reply(desk):
    service, api, clock = await initialized(desk, minutes=5, escalation=ESCALATION)
    service.judge.judge.return_value = Decision("awaiting_handler", "请研发继续排查。")
    human(service, clock, text="物流管理人工客服（TMS）")
    await drain(service)
    service.store.save(
        service.store.tickets["t1"],
        count=1,
        last_send=clock() - 300,
        scenario="awaiting_handler",
        evaluated_generation=service.store.tickets["t1"]["generation"],
    )
    await drain(service)
    assert api.cli.sends[-1][0] == TMS_GROUP

    sends_before = len(api.cli.sends)
    human(service, clock, mid="om_after_escalation", text="研发正在处理，但还没解决")
    await drain(service)
    assert len(api.cli.sends) == sends_before
    assert service.store.tickets["t1"]["count"] == 0

    clock.advance(1800)
    await drain(service)
    assert len(api.cli.sends) == sends_before + 1
    assert api.cli.sends[-1][0] == TMS_GROUP
    assert not api.cli.closes


@pytest.mark.asyncio
async def test_resolution_stops_escalation_and_restart_does_not_duplicate(desk, tmp_path):
    service, api, clock = await desk(minutes=5, path=tmp_path, escalation=ESCALATION)
    add_ticket(service, api, clock)
    await service.reconcile()
    await drain(service)
    service.judge.judge.return_value = Decision("awaiting_handler", "请研发继续排查。")
    human(service, clock, text="供应商关系人工客服（SRM）")
    await drain(service)
    service.store.save(
        service.store.tickets["t1"],
        count=1,
        last_send=clock() - 300,
        scenario="awaiting_handler",
        evaluated_generation=service.store.tickets["t1"]["generation"],
    )
    await drain(service)
    assert service.store.tickets["t1"]["escalation_count"] == 1
    sends_before_restart = len(api.cli.sends)

    await service.close()
    restarted, _, _ = await desk(
        minutes=5,
        path=tmp_path,
        clock=clock,
        api=api,
        escalation=ESCALATION,
    )
    await restarted.reconcile()
    await drain(restarted)
    assert len(api.cli.sends) == sends_before_restart
    assert restarted.store.tickets["t1"]["escalation_count"] == 1

    restarted.judge.judge.return_value = Decision("resolved", reason="业务确认已解决")
    human(restarted, clock, mid="om_resolved", text="已经解决，谢谢")
    await drain(restarted)
    assert restarted.store.tickets["t1"]["status"] == 51
    sends_at_resolution = len(api.cli.sends)
    clock.advance(1800)
    await drain(restarted)
    assert len(api.cli.sends) == sends_at_resolution
    assert restarted.store.tickets["t1"]["escalation_count"] == 1


@pytest.mark.asyncio
async def test_configured_escalation_without_detected_system_does_not_close(desk):
    service, api, clock = await initialized(desk, minutes=5, escalation=ESCALATION)
    item = service.store.tickets["t1"]
    service.store.save(
        item,
        count=1,
        last_send=clock() - 300,
        scenario="awaiting_handler",
        evaluated_generation=item["generation"],
    )
    await drain(service)
    item = service.store.tickets["t1"]
    assert item["status"] == 2
    assert item["escalation_started_at"] is None
    assert item["retry_at"] > clock()
    assert not api.cli.closes
    assert all(send[0] == "oc_ticket" for send in api.cli.sends)


@pytest.mark.asyncio
async def test_escalated_ticket_retries_failed_resolved_close(desk):
    service, api, clock = await initialized(desk, minutes=5, escalation=ESCALATION)
    service.judge.judge.return_value = Decision("awaiting_handler", "请研发继续排查。")
    human(service, clock, text="物流管理人工客服（TMS）")
    await drain(service)
    service.store.save(
        service.store.tickets["t1"],
        count=1,
        last_send=clock() - 300,
        scenario="awaiting_handler",
        evaluated_generation=service.store.tickets["t1"]["generation"],
    )
    await drain(service)
    assert service.store.tickets["t1"]["escalation_count"] == 1

    service.judge.judge.return_value = Decision("resolved", reason="业务确认已解决")
    close = api.cli.close_ticket
    attempts = 0

    async def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise LarkCLIError("close failed", rejected=True)
        return await close(*args, **kwargs)

    api.cli.close_ticket = fail_once
    human(service, clock, mid="om_resolved_retry", text="已经解决了")
    await drain(service)
    assert service.store.tickets["t1"]["status"] == 2
    assert service.store.tickets["t1"]["scenario"] == "resolved"

    clock.advance(5)
    await drain(service)
    assert service.store.tickets["t1"]["status"] == 51
    assert attempts == 2
