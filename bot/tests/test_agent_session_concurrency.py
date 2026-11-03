"""Exercise scheduling through the real bus, AgentLoop and persisted sessions."""

import asyncio
import copy
from collections import defaultdict
from contextlib import asynccontextmanager

import pytest
from vikingbot.agent.context import ContextBuilder
from vikingbot.agent.loop import AgentLoop
from vikingbot.bus.events import InboundMessage, OutboundEventType
from vikingbot.bus.queue import MessageBus
from vikingbot.config.schema import Config, SessionKey
from vikingbot.providers.base import LLMProvider, LLMResponse
from vikingbot.session.manager import SessionManager


class GatedProvider(LLMProvider):
    """Only model calls are gated; context, turn control and saving stay real."""

    def __init__(self):
        super().__init__()
        self.started = defaultdict(asyncio.Event)
        self.gates = {}
        self.calls = []
        self.fail = set()
        self.active = 0
        self.peak = 0
        self.cancelled = asyncio.Event()

    def block(self, text):
        self.gates[text] = asyncio.Event()

    async def chat(self, messages, **kwargs):
        text = messages[-1]["content"]
        self.calls.append((text, copy.deepcopy(messages), kwargs))
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.started[text].set()
        try:
            if text in self.gates:
                await self.gates[text].wait()
            if text in self.fail:
                raise RuntimeError("model call failed")
            return LLMResponse(content=f"answer:{text}")
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        finally:
            self.active -= 1

    def get_default_model(self):
        return "test-model"


@pytest.fixture
def make_concurrent_loop(tmp_path, monkeypatch):
    # No external tools or global hooks are needed for message scheduling.
    monkeypatch.setattr(AgentLoop, "_register_default_tools", lambda self: None)
    monkeypatch.setattr(AgentLoop, "_register_builtin_hooks", lambda self: None)

    def create(limit=4):
        agents = {} if limit is None else {"max_concurrent_sessions": limit}
        provider = GatedProvider()
        loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=tmp_path / "workspace",
            config=Config(storage_workspace=str(tmp_path), agents=agents),
        )
        return loop, provider

    return create


def inbound(text, chat="A", **kwargs):
    return InboundMessage(
        session_key=SessionKey(type="feishu", channel_id="app", chat_id=chat),
        sender_id=f"user-{chat}",
        content=text,
        **kwargs,
    )


def history(loop, chat="A"):
    # A fresh reader verifies the on-disk result, not just the loop's cache.
    session = SessionManager(loop.config.bot_data_path).get_or_create(inbound("", chat).session_key)
    return [msg["content"] for msg in session.messages]


@asynccontextmanager
async def running(loop):
    task = asyncio.create_task(loop.run())
    try:
        yield task
    finally:
        loop.stop()
        await asyncio.wait_for(task, 2)


async def results(bus, count):
    found = []
    while len(found) < count:
        msg = await asyncio.wait_for(bus.consume_outbound(), 2)
        if msg.event_type in (OutboundEventType.RESPONSE, OutboundEventType.INTERRUPT):
            found.append(msg)
    return found


async def ingress_barrier(loop):
    """A different-session callback proves earlier ingress reached the dispatcher."""
    entered = asyncio.Event()

    async def mark():
        entered.set()

    await loop.bus.publish_inbound(inbound("barrier", "barrier", internal_handler=mark))
    await asyncio.wait_for(entered.wait(), 2)


@pytest.mark.asyncio
async def test_other_session_finishes_while_busy_session_backlog_waits(make_concurrent_loop):
    loop, provider = make_concurrent_loop()
    provider.block("A1")
    async with running(loop):
        await loop.bus.publish_inbound(inbound("A1"))
        await asyncio.wait_for(provider.started["A1"].wait(), 2)
        for index in range(2, 22):
            await loop.bus.publish_inbound(inbound(f"A{index}"))
        await loop.bus.publish_inbound(inbound("B1", "B"))
        first = (await results(loop.bus, 1))[0]
        assert first.content == "answer:B1"
        assert history(loop, "B") == ["B1", "answer:B1"]
        provider.gates["A1"].set()
        remaining = await results(loop.bus, 21)
        assert [msg.content for msg in remaining] == [f"answer:A{i}" for i in range(1, 22)]
        assert history(loop) == [text for i in range(1, 22) for text in (f"A{i}", f"answer:A{i}")]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [None, 1])
async def test_default_and_explicit_one_keep_global_fifo(make_concurrent_loop, limit):
    loop, provider = make_concurrent_loop(limit)
    provider.block("A1")
    async with running(loop):
        await loop.bus.publish_inbound(inbound("A1"))
        await asyncio.wait_for(provider.started["A1"].wait(), 2)
        await loop.bus.publish_inbound(inbound("A2"))
        await loop.bus.publish_inbound(inbound("B1", "B"))
        provider.gates["A1"].set()
        assert [msg.content for msg in await results(loop.bus, 3)] == [
            "answer:A1",
            "answer:A2",
            "answer:B1",
        ]
        assert provider.peak == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [2, 4])
async def test_active_model_calls_respect_configured_limit(make_concurrent_loop, limit):
    loop, provider = make_concurrent_loop(limit)
    for index in range(7):
        provider.block(f"turn-{index}")
    async with running(loop):
        for index in range(7):
            await loop.bus.publish_inbound(inbound(f"turn-{index}", str(index)))
        for index in range(limit):
            await asyncio.wait_for(provider.started[f"turn-{index}"].wait(), 2)
        assert provider.active == limit
        for gate in provider.gates.values():
            gate.set()
        replies = await results(loop.bus, 7)
        assert {msg.content for msg in replies} == {f"answer:turn-{i}" for i in range(7)}
        assert provider.peak == limit


@pytest.mark.asyncio
async def test_interrupt_snapshot_spans_bus_and_dispatcher_and_excludes_internal_events(
    make_concurrent_loop,
):
    loop, provider = make_concurrent_loop()
    provider.block("active")

    async def background_result():
        session = loop.sessions.get_or_create(inbound("").session_key)
        session.add_message("assistant", "background result")
        await loop.sessions.save(session)

    async with running(loop):
        await loop.bus.publish_inbound(inbound("active"))
        await asyncio.wait_for(provider.started["active"].wait(), 2)
        await loop.bus.publish_inbound(inbound("queued-1"))
        await loop.bus.publish_inbound(inbound("callback", internal_handler=background_result))
        await loop.bus.publish_inbound(inbound("resume", internal=True))
        await loop.bus.publish_inbound(inbound("queued-2"))
        await ingress_barrier(loop)
        # No yield between this publish and interrupt: queued-3 is still on the bus.
        await loop.bus.publish_inbound(inbound("queued-3"))
        assert await loop.interrupt(inbound("").session_key)
        await loop.bus.publish_inbound(inbound("after"))
        delivered = await results(loop.bus, 6)
        assert sum(msg.event_type == OutboundEventType.INTERRUPT for msg in delivered) == 4
        assert [
            msg.content for msg in delivered if msg.event_type == OutboundEventType.RESPONSE
        ] == ["answer:resume", "answer:after"]
        assert history(loop) == [
            "active",
            "queued-1",
            "background result",
            "resume",
            "answer:resume",
            "queued-2",
            "queued-3",
            "after",
            "answer:after",
        ]


@pytest.mark.asyncio
async def test_interrupt_and_steer_keep_history_and_provenance_in_their_session(
    make_concurrent_loop,
):
    loop, provider = make_concurrent_loop()
    provider.block("A-first")
    provider.block("B-first")
    source_a = {"chat_type": "group", "helpdesk_id": "desk", "helpdesk_ticket_id": "ticket-A"}
    source_b = {"chat_type": "group", "helpdesk_id": "desk", "helpdesk_ticket_id": "ticket-B"}
    async with running(loop):
        await loop.bus.publish_inbound(inbound("A-first", metadata=source_a))
        await loop.bus.publish_inbound(inbound("B-first", "B", metadata=source_b))
        await asyncio.wait_for(provider.started["A-first"].wait(), 2)
        await asyncio.wait_for(provider.started["B-first"].wait(), 2)
        assert loop.steer(inbound("A-steer", metadata=source_a))
        assert await loop.interrupt(inbound("").session_key)
        interrupted = (await results(loop.bus, 1))[0]
        assert interrupted.event_type == OutboundEventType.INTERRUPT
        # A's teardown cannot detach B's active turn.
        assert loop.steer(inbound("B-steer", "B", metadata=source_b))
        provider.gates["B-first"].set()
        reply = (await results(loop.bus, 1))[0]
        assert reply.content == "answer:B-steer"
        assert history(loop) == ["A-first", "A-steer"]
        assert history(loop, "B") == ["B-first", "answer:B-first", "B-steer", "answer:B-steer"]
        for text, messages, _ in provider.calls:
            prompt = str(messages)
            if text.startswith("A-"):
                assert "ticket-A" in prompt and "ticket-B" not in prompt
                assert "B-first" not in prompt
            else:
                assert "ticket-B" in prompt and "ticket-A" not in prompt
                assert "A-first" not in prompt
        await loop.bus.publish_inbound(inbound("A-retry"))
        assert (await results(loop.bus, 1))[0].content == "answer:A-retry"
        retry_prompt = provider.calls[-1][1]
        assert not any(
            str(msg["content"]).startswith(ContextBuilder.MESSAGE_SOURCE_HEADER)
            for msg in retry_prompt
        )


@pytest.mark.asyncio
async def test_rejected_queued_item_does_not_shift_interrupt_to_future_input(make_concurrent_loop):
    loop, provider = make_concurrent_loop()
    provider.block("active")
    checked = asyncio.Event()

    async def expired():
        checked.set()
        return False

    async with running(loop):
        await loop.bus.publish_inbound(inbound("active"))
        await asyncio.wait_for(provider.started["active"].wait(), 2)
        await loop.bus.publish_inbound(inbound("stale", before_process=expired))
        await loop.bus.publish_inbound(inbound("queued"))
        await ingress_barrier(loop)
        assert await loop.interrupt(inbound("").session_key)
        await loop.bus.publish_inbound(inbound("after"))
        delivered = await results(loop.bus, 3)
        assert checked.is_set()
        assert [msg.event_type for msg in delivered] == [
            OutboundEventType.INTERRUPT,
            OutboundEventType.INTERRUPT,
            OutboundEventType.RESPONSE,
        ]
        assert delivered[-1].content == "answer:after"
        assert history(loop) == ["active", "queued", "after", "answer:after"]


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt_queued", [False, True])
async def test_session_remains_owned_through_final_save(
    make_concurrent_loop, monkeypatch, interrupt_queued
):
    loop, provider = make_concurrent_loop()
    saving = asyncio.Event()
    release = asyncio.Event()
    save = loop.sessions.save
    blocked_once = False

    async def gated_save(session):
        nonlocal blocked_once
        if session.key == inbound("").session_key and not blocked_once:
            blocked_once = True
            saving.set()
            await release.wait()
        await save(session)

    monkeypatch.setattr(loop.sessions, "save", gated_save)
    async with running(loop):
        await loop.bus.publish_inbound(inbound("A1"))
        await asyncio.wait_for(saving.wait(), 2)
        # The completed turn is already detached from the interrupt table.
        assert not await loop.interrupt(inbound("").session_key)
        await loop.bus.publish_inbound(inbound("A2"))
        await loop.bus.publish_inbound(inbound("B1", "B"))
        assert (await results(loop.bus, 1))[0].content == "answer:B1"
        assert [text for text, _, _ in provider.calls] == ["A1", "B1"]
        if interrupt_queued:
            assert await loop.interrupt(inbound("").session_key)
        await loop.bus.publish_inbound(inbound("A3"))
        release.set()
        delivered = await results(loop.bus, 3)
        assert delivered[0].content == "answer:A1"
        assert delivered[1].event_type == (
            OutboundEventType.INTERRUPT if interrupt_queued else OutboundEventType.RESPONSE
        )
        assert delivered[2].content == "answer:A3"
        expected = ["A1", "answer:A1", "A2"]
        if not interrupt_queued:
            expected.append("answer:A2")
        assert history(loop) == [*expected, "A3", "answer:A3"]


@pytest.mark.asyncio
async def test_commands_records_callbacks_and_guarded_continuations_share_fifo(
    make_concurrent_loop,
):
    loop, provider = make_concurrent_loop()
    provider.block("first")
    callback_history = []
    guards = []
    valid = True

    async def record_background():
        session = loop.sessions.get_or_create(inbound("").session_key)
        callback_history.extend(msg["content"] for msg in session.messages)
        session.add_message("assistant", "background")
        await loop.sessions.save(session)

    async def still_current():
        guards.append(history(loop))
        return valid

    async with running(loop):
        await loop.bus.publish_inbound(inbound("first"))
        await asyncio.wait_for(provider.started["first"].wait(), 2)
        await loop.bus.publish_inbound(inbound("observed", need_reply=False))
        await loop.bus.publish_inbound(inbound("callback", internal_handler=record_background))
        await loop.bus.publish_inbound(
            inbound("stale", internal=True, before_process=still_current)
        )
        await loop.bus.publish_inbound(inbound("/new"))
        await loop.bus.publish_inbound(inbound("after reset"))
        await ingress_barrier(loop)
        valid = False
        provider.gates["first"].set()
        delivered = await results(loop.bus, 3)
        assert delivered[0].content == "answer:first"
        assert "New session started" in delivered[1].content
        assert delivered[2].content == "answer:after reset"
        assert callback_history == ["first", "answer:first", "observed"]
        assert guards == [[*callback_history, "background"]]
        assert history(loop) == ["after reset", "answer:after reset"]
        assert [text for text, _, _ in provider.calls] == ["first", "after reset"]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 4])
async def test_message_failure_allows_other_session_and_same_session_retry(
    make_concurrent_loop, limit
):
    loop, provider = make_concurrent_loop(limit)
    provider.fail.add("failure")
    async with running(loop):
        for msg in (inbound("failure"), inbound("retry"), inbound("other", "B")):
            await loop.bus.publish_inbound(msg)
        delivered = await results(loop.bus, 3)
        assert sorted(msg.content for msg in delivered) == [
            "Sorry, I encountered an error: model call failed",
            "answer:other",
            "answer:retry",
        ]
        assert history(loop) == ["retry", "answer:retry"]
        assert history(loop, "B") == ["other", "answer:other"]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 4])
async def test_cancelled_internal_item_does_not_lose_execution_capacity(
    make_concurrent_loop, limit
):
    loop, _ = make_concurrent_loop(limit)

    async def cancelled_callback():
        future = asyncio.get_running_loop().create_future()
        future.cancel()
        await future

    async with running(loop):
        await loop.bus.publish_inbound(inbound("callback", internal_handler=cancelled_callback))
        await loop.bus.publish_inbound(inbound("retry"))
        delivered = await results(loop.bus, 2)
        assert [msg.content for msg in delivered] == [
            "Sorry, I encountered an error: CancelledError",
            "answer:retry",
        ]
        assert history(loop) == ["retry", "answer:retry"]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 4])
async def test_interrupt_persists_input_then_processes_next_turn(make_concurrent_loop, limit):
    loop, provider = make_concurrent_loop(limit)
    provider.block("active")
    async with running(loop):
        await loop.bus.publish_inbound(inbound("active"))
        await asyncio.wait_for(provider.started["active"].wait(), 2)
        assert await loop.interrupt(inbound("").session_key)
        interrupted = (await results(loop.bus, 1))[0]
        assert interrupted.event_type == OutboundEventType.INTERRUPT
        assert history(loop) == ["active"]
        await loop.bus.publish_inbound(inbound("after"))
        assert (await results(loop.bus, 1))[0].content == "answer:after"
        assert history(loop) == ["active", "after", "answer:after"]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 4])
@pytest.mark.parametrize("cancel_run", [False, True])
async def test_shutdown_awaits_callback_that_suppresses_cancellation(
    make_concurrent_loop, limit, cancel_run
):
    loop, provider = make_concurrent_loop(limit)
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleaned = asyncio.Event()
    closed = asyncio.Event()

    async def callback():
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cleaning.set()
            await release_cleanup.wait()
            cleaned.set()

    class Extension:
        async def start(self):
            pass

        async def close(self):
            assert cleaned.is_set()
            closed.set()

    loop.runtime_extensions.extensions.append(Extension())
    task = asyncio.create_task(loop.run())
    try:
        await loop.bus.publish_inbound(inbound("callback", internal_handler=callback))
        await asyncio.wait_for(entered.wait(), 2)
        await loop.bus.publish_inbound(inbound("queued"))
        if cancel_run:
            task.cancel()
        else:
            loop.stop()
        await asyncio.wait_for(cleaning.wait(), 2)
        assert not task.done()
        assert not closed.is_set()
        release_cleanup.set()
        if cancel_run:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        else:
            await asyncio.wait_for(task, 2)
        assert closed.is_set()
        assert not provider.calls
        assert loop._session_dispatcher is None
        assert loop._message_consumer_task is None
        assert not loop._active_turns
    finally:
        release_cleanup.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 4])
@pytest.mark.parametrize("stop_first", [False, True])
async def test_run_cancellation_after_user_interrupt_still_propagates(
    make_concurrent_loop, limit, stop_first
):
    loop, provider = make_concurrent_loop(limit)
    provider.block("active")
    task = asyncio.create_task(loop.run())
    try:
        await loop.bus.publish_inbound(inbound("active"))
        await asyncio.wait_for(provider.started["active"].wait(), 2)
        assert await loop.interrupt(inbound("").session_key)
        if stop_first:
            loop.stop()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert provider.active == 0
        assert not loop._active_turns
        assert loop._session_dispatcher is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 4])
@pytest.mark.parametrize("cancel_run", [False, True])
async def test_shutdown_awaits_active_and_steer_cleanup_before_closing_extensions(
    make_concurrent_loop, monkeypatch, limit, cancel_run
):
    loop, provider = make_concurrent_loop(limit)
    provider.block("active")
    preparing = asyncio.Event()
    prepared_cleanup = asyncio.Event()
    closed = asyncio.Event()

    async def prepare(msg):
        preparing.set()
        try:
            await asyncio.Future()
        finally:
            prepared_cleanup.set()

    class Extension:
        async def start(self):
            pass

        async def close(self):
            assert provider.cancelled.is_set()
            assert prepared_cleanup.is_set()
            assert provider.active == 0
            closed.set()

    loop.runtime_extensions.extensions.append(Extension())
    monkeypatch.setattr(loop, "_prepare_steer_message", prepare)
    task = asyncio.create_task(loop.run())
    try:
        await loop.bus.publish_inbound(inbound("active"))
        await asyncio.wait_for(provider.started["active"].wait(), 2)
        assert loop.steer(inbound("steer"))
        await asyncio.wait_for(preparing.wait(), 2)
        await loop.bus.publish_inbound(inbound("queued"))
        if cancel_run:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        else:
            loop.stop()
            await asyncio.wait_for(task, 2)
        assert closed.is_set()
        assert [text for text, _, _ in provider.calls] == ["active"]
        await loop.close_mcp()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 4])
async def test_stop_wakes_idle_ingress_and_workers(make_concurrent_loop, limit):
    loop, _ = make_concurrent_loop(limit)
    task = asyncio.create_task(loop.run())
    try:
        await ingress_barrier(loop)
        loop.stop()
        await asyncio.wait_for(task, 2)
        loop.stop()
        await loop.close_mcp()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
