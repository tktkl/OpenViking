"""Helpdesk callbacks use the real AgentLoop capacity and physical-chat saving guards."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from test_agent_session_concurrency import (
    inbound,
    results,
    running,
)
from test_agent_session_concurrency import (
    make_concurrent_loop as make_concurrent_loop,
)
from test_helpdesk_followup import add_ticket, human
from test_helpdesk_followup import desk as desk
from vikingbot.bus.events import InboundMessage, OutboundMessage
from vikingbot.config.schema import SessionKey
from vikingbot.extensions.feishu_helpdesk.decision import Decision


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 4])
async def test_main_answer_and_helpdesk_share_worker_capacity(make_concurrent_loop, desk, limit):
    loop, provider = make_concurrent_loop(limit)
    service, api, clock = await desk(bus=loop.bus, session_busy=loop.has_chat_work)
    add_ticket(service, api, clock)
    await service.reconcile()
    # Skip only the welcome here: exercise a real judgement inside the dispatcher.
    item = service.store.tickets["t1"]
    service.store.save(item, welcome_at=clock())
    human(service, clock)
    provider.block("main")
    judged = asyncio.Event()

    async def judge(*args):
        await provider.chat([{"role": "user", "content": "helpdesk"}])
        judged.set()
        return Decision("awaiting_handler", "请处理方跟进。")

    service.judge.judge = judge
    async with running(loop):
        await loop.bus.publish_inbound(inbound("main", "oc_other"))
        await asyncio.wait_for(provider.started["main"].wait(), 2)
        await service.tick()
        if limit == 1:
            await asyncio.sleep(0.02)
            assert not judged.is_set()
            provider.gates["main"].set()
            await asyncio.wait_for(judged.wait(), 2)
            assert provider.peak == 1
        else:
            await asyncio.wait_for(judged.wait(), 2)
            assert provider.peak == 2
            provider.gates["main"].set()
        await results(loop.bus, 1)
    assert not api.cli.closes


@pytest.mark.asyncio
@pytest.mark.parametrize("topic", [False, True])
async def test_final_save_and_topics_prevent_closing(
    make_concurrent_loop, desk, monkeypatch, topic
):
    loop, provider = make_concurrent_loop(4)
    service, api, clock = await desk(bus=loop.bus, session_busy=loop.has_chat_work)
    add_ticket(service, api, clock)
    await service.reconcile()
    key = service._key(service.store.tickets["t1"])
    chat_key = SessionKey(
        type=key.type,
        channel_id=key.channel_id,
        chat_id=key.chat_id + ("#om_topic" if topic else ""),
    )
    saving, release, judged = asyncio.Event(), asyncio.Event(), asyncio.Event()
    saved = loop.sessions.save

    async def save(session):
        if session.key == chat_key and not saving.is_set():
            saving.set()
            await release.wait()
        await saved(session)

    monkeypatch.setattr(loop.sessions, "save", save)
    service.store.save(service.store.tickets["t1"], welcome_at=clock())
    human(service, clock)

    async def judge(*args):
        judged.set()
        return Decision("resolved", reason="已恢复")

    service.judge.judge = judge
    async with running(loop):
        await loop.bus.publish_inbound(
            InboundMessage(session_key=chat_key, sender_id="ou_guest", content="answer")
        )
        await asyncio.wait_for(saving.wait(), 2)
        assert loop.has_chat_work(key)
        await service.tick()
        if topic:
            await asyncio.wait_for(judged.wait(), 2)
            # Let the callback finish its busy check while the other topic saves.
            while service._queued:
                await asyncio.sleep(0)
            assert not api.cli.closes
        else:
            await asyncio.sleep(0.02)
            assert not judged.is_set()
        release.set()
        await results(loop.bus, 1)
        if not topic:
            await asyncio.wait_for(judged.wait(), 2)
        while service._queued:
            await asyncio.sleep(0)
        clock.advance(10)
        await service.tick()
        while service._queued:
            await asyncio.sleep(0)
        assert len(api.cli.closes) == 1


@pytest.mark.asyncio
async def test_ticket_wide_busy_query_includes_bus_and_dispatcher_without_other_channels(
    make_concurrent_loop,
):
    loop, provider = make_concurrent_loop(4)
    key = inbound("", "oc_ticket").session_key
    await loop.bus.publish_inbound(
        inbound("topic", "oc_ticket#root", internal=True, internal_handler=AsyncMock())
    )
    assert loop.has_chat_work(key)
    assert not loop.has_chat_work(
        SessionKey(type="feishu", channel_id="another_app", chat_id="oc_ticket")
    )
    loop.bus.inbound.get_nowait()
    async with running(loop):
        provider.block("blocked")
        await loop.bus.publish_inbound(inbound("blocked", "oc_ticket#root"))
        await provider.started["blocked"].wait()
        await loop.bus.publish_inbound(
            inbound("queued", "oc_ticket#root", internal_handler=AsyncMock())
        )
        assert loop.has_chat_work(key)
        provider.gates["blocked"].set()
        await results(loop.bus, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 4])
@pytest.mark.parametrize("topic", [False, True])
async def test_direct_call_and_its_final_save_prevent_closing(
    make_concurrent_loop, desk, monkeypatch, limit, topic
):
    loop, provider = make_concurrent_loop(limit)
    service, api, clock = await desk(bus=loop.bus, session_busy=loop.has_chat_work)
    add_ticket(service, api, clock)
    await service.reconcile()
    service.store.save(service.store.tickets["t1"], welcome_at=clock())
    human(service, clock)
    service.judge.judge.return_value = Decision("resolved", reason="业务已确认恢复")
    key = service._key(service.store.tickets["t1"])
    direct_key = SessionKey(
        type=key.type,
        channel_id=key.channel_id,
        chat_id=key.chat_id + ("#om_topic" if topic else ""),
    )
    saving, release_save = asyncio.Event(), asyncio.Event()
    original_save = loop.sessions.save

    async def save(session):
        if session.key == direct_key:
            saving.set()
            await release_save.wait()
        await original_save(session)

    monkeypatch.setattr(loop.sessions, "save", save)
    provider.block("scheduled")

    async def attempt_close():
        await service.tick()

        async def idle():
            while service._queued:
                await asyncio.sleep(0)

        await asyncio.wait_for(idle(), 2)

    async with running(loop):
        direct = asyncio.create_task(loop.process_direct("scheduled", session_key=direct_key))
        try:
            await asyncio.wait_for(provider.started["scheduled"].wait(), 2)
            assert loop.has_chat_work(key)
            await attempt_close()
            assert not api.cli.closes

            provider.gates["scheduled"].set()
            await asyncio.wait_for(saving.wait(), 2)
            assert loop.has_chat_work(key)
            clock.advance(10)
            await attempt_close()
            assert not api.cli.closes

            release_save.set()
            assert await asyncio.wait_for(direct, 2) == "answer:scheduled"
            assert not loop.has_chat_work(key)
            clock.advance(10)
            await attempt_close()
            assert len(api.cli.closes) == 1
        finally:
            release_save.set()
            direct.cancel()
            await asyncio.gather(direct, return_exceptions=True)


@pytest.mark.asyncio
async def test_direct_call_is_busy_during_mcp_setup_and_cleans_up_on_cancel(
    make_concurrent_loop, monkeypatch
):
    loop, _ = make_concurrent_loop(4)
    key = inbound("", "oc_ticket#topic").session_key
    entered = asyncio.Event()

    async def connect():
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(loop, "_connect_mcp", connect)
    direct = asyncio.create_task(loop.process_direct("scheduled", session_key=key))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert loop.has_chat_work(inbound("", "oc_ticket").session_key)
        assert not loop.has_chat_work(
            SessionKey(type=key.type, channel_id="other_app", chat_id="oc_ticket")
        )
    finally:
        direct.cancel()
        await asyncio.gather(direct, return_exceptions=True)
    assert not loop.has_chat_work(key)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["cancel", "error"])
async def test_overlapping_direct_calls_remain_busy_until_last_cleanup(
    make_concurrent_loop, monkeypatch, outcome
):
    loop, _ = make_concurrent_loop(4)
    key = inbound("", "oc_ticket").session_key
    gates = {name: asyncio.Event() for name in ("first", "second")}
    started = {name: asyncio.Event() for name in gates}

    async def process(msg):
        started[msg.content].set()
        await gates[msg.content].wait()
        if msg.content == "second":
            raise RuntimeError("direct call failed")
        return OutboundMessage(session_key=msg.session_key, content="done")

    monkeypatch.setattr(loop, "_process_message", process)
    tasks = [asyncio.create_task(loop.process_direct(name, session_key=key)) for name in gates]
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 2)
        assert loop.has_chat_work(key)
        gates["first"].set()
        assert await asyncio.wait_for(tasks[0], 2) == "done"
        assert loop.has_chat_work(key)
        if outcome == "cancel":
            tasks[1].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[1]
        else:
            gates["second"].set()
            with pytest.raises(RuntimeError, match="direct call failed"):
                await tasks[1]
        assert not loop.has_chat_work(key)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
