import asyncio
import json

import pytest
from pydantic import ValidationError
from vikingbot.agent.session_dispatcher import SessionDispatcher
from vikingbot.bus.events import InboundMessage
from vikingbot.config import loader
from vikingbot.config.schema import AgentsConfig, Config, SessionKey


def message(chat, content="input", **kwargs):
    return InboundMessage(
        session_key=SessionKey(type="feishu", channel_id="app", chat_id=chat),
        sender_id="user",
        content=content,
        **kwargs,
    )


def test_session_concurrency_configuration():
    assert AgentsConfig().max_concurrent_sessions == 1
    assert Config(agents={"max_concurrent_sessions": 4}).agents.max_concurrent_sessions == 4
    for invalid in (0, -1, 1.5, True, "4"):
        with pytest.raises(ValidationError):
            AgentsConfig(max_concurrent_sessions=invalid)


@pytest.mark.parametrize(
    "agents, expected",
    [({}, 1), ({"max_concurrent_sessions": 1}, 1), ({"max_concurrent_sessions": 4}, 4)],
)
def test_session_concurrency_loads_from_ov_conf(tmp_path, monkeypatch, agents, expected):
    path = tmp_path / "ov.conf"
    path.write_text(json.dumps({"bot": {"agents": agents}}))
    monkeypatch.setattr(loader, "CONFIG_PATH", path)
    assert loader.load_config().agents.max_concurrent_sessions == expected


@pytest.mark.asyncio
async def test_busy_session_backlog_leaves_workers_for_other_sessions():
    started = asyncio.Event()
    release = asyncio.Event()
    other_finished = asyncio.Event()
    completed = []

    async def handle(msg):
        if msg.content == "blocked":
            started.set()
            await release.wait()
        completed.append(msg.content)
        if msg.content == "other":
            other_finished.set()

    dispatcher = SessionDispatcher(4, handle)
    dispatcher.start()
    try:
        dispatcher.submit(message("A", "blocked"))
        await asyncio.wait_for(started.wait(), 2)
        for index in range(100):
            dispatcher.submit(message("A", f"queued-{index}"))
        dispatcher.submit(message("B", "other"))
        await asyncio.wait_for(other_finished.wait(), 2)
        assert completed == ["other"]
        release.set()
        await asyncio.wait_for(dispatcher.join(), 2)
        assert completed == ["other", "blocked", *(f"queued-{i}" for i in range(100))]
    finally:
        await dispatcher.close()


@pytest.mark.asyncio
async def test_ready_sessions_rotate_after_one_item():
    started = asyncio.Event()
    release = asyncio.Event()
    completed = []

    async def handle(msg):
        if msg.content == "A1":
            started.set()
            await release.wait()
        completed.append(msg.content)

    dispatcher = SessionDispatcher(1, handle)
    dispatcher.start()
    try:
        dispatcher.submit(message("A", "A1"))
        await asyncio.wait_for(started.wait(), 2)
        dispatcher.submit(message("A", "A2"))
        dispatcher.submit(message("A", "A3"))
        dispatcher.submit(message("B", "B1"))
        release.set()
        await asyncio.wait_for(dispatcher.join(), 2)
        assert completed == ["A1", "B1", "A2", "A3"]
    finally:
        await dispatcher.close()


@pytest.mark.asyncio
async def test_worker_limit_and_full_session_identity():
    release = asyncio.Event()
    saturated = asyncio.Event()
    active = set()
    peak = 0
    completed = []

    async def handle(msg):
        nonlocal peak
        assert msg.session_key not in active
        active.add(msg.session_key)
        peak = max(peak, len(active))
        if len(active) == 4:
            saturated.set()
        try:
            await release.wait()
            completed.append(msg.content)
        finally:
            active.remove(msg.session_key)

    dispatcher = SessionDispatcher(4, handle)
    dispatcher.start()
    try:
        # Same chat_id across two apps and two channels is four separate sessions.
        for channel in ("feishu", "openapi"):
            for app in ("one", "two"):
                for index in range(3):
                    dispatcher.submit(
                        InboundMessage(
                            session_key=SessionKey(type=channel, channel_id=app, chat_id="same"),
                            sender_id="user",
                            content=f"{channel}-{app}-{index}",
                        )
                    )
        await asyncio.wait_for(saturated.wait(), 2)
        release.set()
        await asyncio.wait_for(dispatcher.join(), 2)
        assert peak == 4
        assert len(completed) == 12
    finally:
        await dispatcher.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_failed_item_does_not_prevent_same_session_retry(error):
    completed = []

    async def handle(msg):
        if msg.content == "fail":
            raise error("broken item")
        completed.append(msg.content)

    dispatcher = SessionDispatcher(2, handle)
    dispatcher.start()
    try:
        dispatcher.submit(message("A", "fail"))
        dispatcher.submit(message("A", "retry"))
        dispatcher.submit(message("B", "other"))
        await asyncio.wait_for(dispatcher.join(), 2)
        assert sorted(completed) == ["other", "retry"]
    finally:
        await dispatcher.close()


@pytest.mark.asyncio
async def test_close_awaits_cleanup_and_discards_waiting_work():
    started = asyncio.Event()
    cleaned = asyncio.Event()
    completed = []

    async def handle(msg):
        started.set()
        try:
            await asyncio.Future()
            completed.append(msg.content)
        finally:
            cleaned.set()

    dispatcher = SessionDispatcher(2, handle)
    dispatcher.start()
    dispatcher.submit(message("A", "active"))
    await asyncio.wait_for(started.wait(), 2)
    dispatcher.submit(message("A", "user"))
    dispatcher.submit(message("A", "internal", internal=True))
    dispatcher.submit(message("A", "callback", internal_handler=lambda: asyncio.sleep(0)))
    assert dispatcher.count_pending(message("A").session_key) == 1
    await asyncio.wait_for(dispatcher.close(), 2)
    assert cleaned.is_set()
    assert completed == []
    await dispatcher.close()
    await dispatcher.join()


@pytest.mark.asyncio
async def test_close_finishes_when_handler_suppresses_cancellation():
    started = asyncio.Event()
    cleaning = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleaned = asyncio.Event()
    executed = []

    async def handle(msg):
        executed.append(msg.content)
        if msg.content != "active":
            return
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cleaning.set()
            await release_cleanup.wait()
            cleaned.set()

    dispatcher = SessionDispatcher(1, handle)
    dispatcher.start()
    dispatcher.submit(message("A", "active"))
    await asyncio.wait_for(started.wait(), 2)
    dispatcher.submit(message("A", "same-session"))
    dispatcher.submit(message("B", "other-session"))
    closing = asyncio.create_task(dispatcher.close())
    try:
        await asyncio.wait_for(cleaning.wait(), 2)
        assert not closing.done()
        release_cleanup.set()
        await asyncio.wait_for(closing, 2)
        await asyncio.wait_for(dispatcher.join(), 2)
        assert cleaned.is_set()
        assert executed == ["active"]
        assert not dispatcher._workers
        assert not dispatcher._pending
    finally:
        release_cleanup.set()
        closing.cancel()
        await asyncio.gather(closing, return_exceptions=True)
        await dispatcher.close()
