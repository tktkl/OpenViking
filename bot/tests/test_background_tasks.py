"""Local watcher tests: no real model, MCP, Feishu API or business systems."""

import asyncio
import copy
import hashlib
import json
import shlex
import stat
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from vikingbot.agent.tools.base import ToolContext
from vikingbot.agent.tools.registry import ToolRegistry
from vikingbot.bus.events import InboundMessage, OutboundMessage
from vikingbot.bus.queue import MessageBus
from vikingbot.channels.manager import ChannelManager
from vikingbot.config.schema import Config, SessionKey
from vikingbot.extensions.background_tasks.service import BackgroundTasks
from vikingbot.extensions.runtime import RuntimeContext, RuntimeExtensions
from vikingbot.session.manager import SessionManager

TASK = "a" * 32
RUN = "b" * 32
ORIGIN = SessionKey(type="feishu", channel_id="app", chat_id="original-thread")


class FakeSandbox:
    def __init__(self):
        self.state = "running"
        self.run = RUN
        self.calls = []
        self.error = None
        self.command_failed = False
        self.outcome = {"status": "completed", "text": "修复完成，3 项本地检查通过。"}
        self.protocol_version = 2
        self.mutate_page = None
        self.ready = True
        self.files = {}

    async def write_file_bytes(self, path, content):
        self.files[path] = content

    async def read_file_bytes(self, path, max_bytes):
        return self.files[path][:max_bytes + 1]

    async def remove_tree(self, path):
        self.files = {key: value for key, value in self.files.items() if not key.startswith(path + "/")}

    async def execute(self, command, timeout):
        argv = shlex.split(command)
        # Ignore the extension's static POSIX mkdir/umask setup, while retaining
        # exact runner argv to test quoting and original-sandbox execution.
        sink = argv.index(">")
        path = argv[sink + 1]
        marker = argv[-1]
        prefix = argv[:sink]
        argv = prefix[max(i for i, token in enumerate(prefix) if token == "&&") + 1:]
        self.calls.append((argv, timeout))
        if self.command_failed:
            return "Exit code: 1"  # leave an old probe file in place
        if self.error:
            self.files[path] = self.error.encode()
            return marker
        action = next(a for a in argv if a in {"status", "result", "logs", "stop"})
        if action == "stop":
            if argv[argv.index("--expected-run-id") + 1] != self.run:
                return "Exit code: 1"  # validation happens BEFORE state change
            self.state = "stopped"
        value = {"task_id": TASK, "run_id": self.run, "state": self.state,
                 "protocol_version": self.protocol_version}
        if action == "result":
            text = self.outcome["text"] if self.outcome else "任务已结束，但最终答复不符合约定格式。"
            encoded = text.encode("utf-8")
            offset = int(argv[argv.index("--offset") + 1])
            chunk = encoded[offset:offset + 8000].decode("utf-8", errors="ignore")
            end = offset + len(chunk.encode("utf-8"))
            value.update(ready=self.ready, text=chunk, offset=offset, next_offset=end,
                         eof=end == len(encoded), total_bytes=len(encoded),
                         sha256=hashlib.sha256(encoded).hexdigest(), source_truncated=False,
                         outcome_error=None if self.outcome else "invalid_structured_result",
                         outcome_status=self.outcome["status"] if self.outcome else None)
            if self.mutate_page:
                self.mutate_page(value)
        elif action == "logs":
            value["output"] = {"text": "bounded log", "truncated": True}
        self.files[path] = json.dumps(value).encode()
        return marker


@pytest.fixture
async def setup(tmp_path):
    config = Config(storage_workspace=str(tmp_path))
    config.sandbox.backends.direct.allow_exec = True
    sandbox = FakeSandbox()
    manager = SimpleNamespace(get_sandbox=AsyncMock(return_value=sandbox))
    host = RuntimeContext(config, tmp_path / "workspace", MessageBus(), ToolRegistry(config=config),
                          SessionManager(config.bot_data_path), manager)
    service = BackgroundTasks(host, {"poll_interval": 300, "delivery_retry_seconds": 0.05,
                                     "max_probe_errors": 2, "max_delivery_attempts": 2})
    await service.start()
    # Drive deterministic ticks without waiting for a clock or real CLI process.
    service.worker.cancel()
    await asyncio.gather(service.worker, return_exceptions=True)
    service.worker = None
    context = ToolContext(session_key=ORIGIN, sender_id="original-user", actor_peer_id="peer",
                          sandbox_manager=manager, channel_metadata={
                              "reply_to": "original-chat", "message_id": "original-message",
                              "root_id": "original-root", "chat_mode": "thread", "source": "forged",
                              "Authorization": "SECRET", "helpdesk_ticket_id": "ticket",
                              "helpdesk_id": "desk", "chat_type": "group",
                          }, openviking_connection={"api_key": "SECRET"})
    yield service, context, sandbox
    await service.close()


async def watch(service, context, runner=None):
    value = await service.watch(context, TASK, runner or ["python3", "/skills/runner.py", "--workspace", "/workspace"])
    return service.items[value["id"]]


async def deliver(service, item):
    await service.tick(item)
    event = await asyncio.wait_for(service.host.bus.consume_inbound(), 1)
    assert event.internal_handler and event.internal and not event.need_reply
    await event.internal_handler()
    return await asyncio.wait_for(service.host.bus.consume_outbound(), 1)


async def test_completion_goes_to_saved_thread_and_history_without_model_turn(setup):
    service, context, sandbox = setup
    item = await watch(service, context)
    sandbox.state = "succeeded"
    msg = await deliver(service, item)
    assert msg.session_key == ORIGIN
    assert msg.metadata["message_id"] == "original-message"
    assert msg.metadata["root_id"] == "original-root"
    assert "source" not in msg.metadata and "Authorization" not in msg.metadata
    assert "helpdesk_id" not in msg.metadata and "helpdesk_ticket_id" not in msg.metadata
    assert "修复完成" in msg.content
    await msg.on_delivery(True)
    assert item["delivery"] == "sent"
    await service.tick(item)
    assert service.host.bus.inbound_size == 0
    history = service.host.sessions.get_or_create(ORIGIN).messages
    assert len(history) == 1 and history[0]["content"] == msg.content
    journal = (service.directory / (item["id"] + ".json"))
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600
    assert "SECRET" not in journal.read_text()


async def test_launcher_return_does_not_count_as_task_completion(setup):
    service, context, _ = setup
    item = await watch(service, context)
    await service.tick(item)
    assert item["state"] == "running" and item["text"] is None
    assert service.host.bus.inbound_size == 0


@pytest.mark.parametrize("outcome", ["needs_input", "blocked"])
async def test_codex_questions_deliver_directly_without_engineering_rewrite(setup, outcome):
    service, context, sandbox = setup
    sandbox.outcome = {"status": outcome, "text": "请确认冲突的目标环境。"}
    item = await watch(service, context)
    sandbox.state = "succeeded"
    msg = await deliver(service, item)
    assert "请确认冲突的目标环境" in msg.content
    await msg.on_delivery(True)
    await service.tick(item)
    assert service.host.bus.inbound_size == 0


async def test_invalid_structured_result_is_never_reported_as_completed(setup):
    service, context, sandbox = setup
    sandbox.outcome = None
    item = await watch(service, context)
    sandbox.state = "succeeded"
    msg = await deliver(service, item)
    assert "不符合约定格式" in msg.content


@pytest.mark.parametrize("state", ["failed", "timed_out", "interrupted", "log_limit", "stopped"])
async def test_process_failures_are_not_business_success(setup, state):
    service, context, sandbox = setup
    item = await watch(service, context)
    sandbox.state = state
    assert f"任务状态：{state}" in (await deliver(service, item)).content


async def test_watch_is_idempotent_and_foreign_session_or_actor_cannot_control(setup):
    service, context, _ = setup
    item = await watch(service, context)
    assert (await watch(service, context))["id"] == item["id"]
    tool = service.host.tools.get("background_task")
    other = ToolContext(session_key=SessionKey(type="feishu", channel_id="app", chat_id="other"), sender_id="x")
    assert (await tool.execute(other, "stop", watch_id=item["id"])).startswith("Error:")
    with pytest.raises(ValueError, match="different session"):
        await watch(service, other)
    stranger = ToolContext(session_key=ORIGIN, sender_id="x", actor_peer_id="different-peer")
    assert "originating user" in await tool.execute(stranger, "stop", watch_id=item["id"])


async def test_argv_metacharacters_are_literal_and_commands_use_original_sandbox(setup):
    service, context, sandbox = setup
    prefix = ["/python space", "/runner $literal;$(touch NO).py", "--workspace", "/a ' b"]
    item = await watch(service, context, prefix)
    await service.tick(item)
    assert sandbox.calls[0][0] == prefix + ["status", TASK]
    assert sandbox.calls[1][0] == prefix + ["status", TASK, "--run-id", RUN]
    service.host.sandbox_manager.get_sandbox.assert_awaited_with(ORIGIN)


async def test_delivery_retries_are_bounded_and_history_is_deduplicated(setup):
    service, context, sandbox = setup
    item = await watch(service, context)
    sandbox.state = "succeeded"
    msg = await deliver(service, item)
    await msg.on_delivery(False)
    item["next_delivery"] = 0
    retry = await deliver(service, item)
    await retry.on_delivery(False)
    assert item["delivery"] == "failed"
    assert len(service.host.sessions.get_or_create(ORIGIN).messages) == 1
    await service.tick(item)
    assert service.host.bus.inbound_size == 0


async def test_missing_receipt_does_not_wait_forever(setup):
    service, context, sandbox = setup
    item = await watch(service, context)
    sandbox.state = "succeeded"
    await deliver(service, item)
    item["receipt_deadline"] = time.time() - 1
    await deliver(service, item)
    item["receipt_deadline"] = time.time() - 1
    await service.tick(item)
    assert item["delivery"] == "failed"


async def test_restart_recovers_outbox_and_reconciles_without_relaunch(setup):
    service, context, sandbox = setup
    item = await watch(service, context)
    sandbox.state = "succeeded"
    await deliver(service, item)  # simulate crash before delivery receipt
    await service.close()
    await service.start()
    service.worker.cancel()
    await asyncio.gather(service.worker, return_exceptions=True)
    service.worker = None
    recovered = service.items[item["id"]]
    recovered["receipt_deadline"] = 0
    msg = await deliver(service, recovered)
    await msg.on_delivery(True)
    assert len(service.host.sessions.get_or_create(ORIGIN).messages) == 1
    assert all(any(a in {"status", "result"} for a in call[0]) for call in sandbox.calls)


async def test_probe_failure_pauses_then_retry_gets_real_result(setup):
    service, context, sandbox = setup
    item = await watch(service, context)
    sandbox.error = "STDERR: private detail\nExit code: 1"
    await service.tick(item)
    msg = await deliver(service, item)
    assert "可能仍在运行" in msg.content and "private detail" not in msg.content
    await msg.on_delivery(True)
    sandbox.error = None
    sandbox.state = "succeeded"
    tool = service.host.tools.get("background_task")
    await tool.execute(context, "retry", watch_id=item["id"])
    msg = await deliver(service, item)
    assert "修复完成" in msg.content
    assert len(service.host.sessions.get_or_create(ORIGIN).messages) == 2


async def test_queue_additions_only_then_continuation_after_confirmed_delivery(setup):
    service, context, sandbox = setup
    item = await watch(service, context)
    tool = service.host.tools.get("background_task")
    await tool.execute(context, "enqueue", watch_id=item["id"], text="请补充边界测试")
    assert sandbox.calls[-1][0][-2] == "status"  # no interactive stdin command
    sandbox.state = "succeeded"
    msg = await deliver(service, item)
    await service.tick(item)
    assert service.host.bus.inbound_size == 0  # not before delivery ack
    await msg.on_delivery(True)
    await service.tick(item)
    continuation = await service.host.bus.consume_inbound()
    assert continuation.internal and continuation.need_reply and continuation.internal_handler is None
    assert "请补充边界测试" in continuation.content
    assert continuation.actor_peer_id == context.actor_peer_id
    assert "source" not in continuation.metadata
    await service.tick(item)
    assert service.host.bus.inbound_size == 0  # no replay of follow-up writes
    await tool.execute(context, "ack", watch_id=item["id"])
    assert item["queued"] == []


async def test_stop_cancels_auto_continuation_and_logs_are_bounded(setup):
    service, context, sandbox = setup
    item = await watch(service, context)
    tool = service.host.tools.get("background_task")
    await tool.execute(context, "enqueue", watch_id=item["id"], text="more")
    await tool.execute(context, "logs", watch_id=item["id"])
    assert sandbox.calls[-1][0][-4:] == ["--lines", "100", "--max-bytes", "65536"]
    await tool.execute(context, "stop", watch_id=item["id"])
    msg = await deliver(service, item)
    await msg.on_delivery(True)
    await service.tick(item)
    assert service.host.bus.inbound_size == 0
    assert item["continuation"] == "cancelled"


async def test_changed_run_never_delivers_another_runs_answer(setup):
    service, context, sandbox = setup
    item = await watch(service, context)
    sandbox.run = "c" * 32
    sandbox.state = "succeeded"
    await service.tick(item)
    msg = await deliver(service, item)
    assert "暂停自动监测" in msg.content and "修复完成" not in msg.content


async def test_single_gateway_owns_journal_and_disabled_exec_is_rejected(setup):
    service, _, _ = setup
    host = service.host
    other_host = RuntimeContext(host.config, host.workspace, MessageBus(), ToolRegistry(config=host.config),
                                host.sessions, host.sandbox_manager)
    other = BackgroundTasks(other_host, {})
    with pytest.raises(BlockingIOError):
        await other.start()
    await other.close()
    host.config.sandbox.backends.direct.allow_exec = False
    with pytest.raises(ValueError, match="allow_exec"):
        BackgroundTasks(other_host, {})


async def test_offline_watch_refuses_to_promise_automatic_delivery(setup):
    service, context, _ = setup
    await service.close()
    with pytest.raises(ValueError, match="live gateway"):
        await watch(service, context)


async def test_forget_requires_terminal_delivery_and_only_cleans_bridge_files(setup):
    service, context, sandbox = setup
    item = await watch(service, context)
    tool = service.host.tools.get("background_task")
    assert (await tool.execute(context, "forget", watch_id=item["id"])).startswith("Error:")
    sandbox.files["tmp/wangoon/codex/tasks/retained-answer.txt"] = b"keep"
    sandbox.state = "succeeded"
    msg = await deliver(service, item)
    await msg.on_delivery(True)
    result = json.loads(await tool.execute(context, "forget", watch_id=item["id"]))
    assert result["forgotten"] == item["id"]
    assert item["id"] not in service.items
    assert not (service.directory / (item["id"] + ".json")).exists()
    assert sandbox.files["tmp/wangoon/codex/tasks/retained-answer.txt"] == b"keep"


@pytest.mark.parametrize("return_value", [True, False, None, RuntimeError("send failed")])
async def test_generic_dispatcher_reports_actual_channel_receipt(return_value):
    bus = MessageBus()
    manager = ChannelManager(bus)
    send = AsyncMock(side_effect=return_value) if isinstance(return_value, Exception) else AsyncMock(return_value=return_value)
    manager.channels[ORIGIN.channel_key()] = SimpleNamespace(send=send)
    receipt = AsyncMock()
    await bus.publish_outbound(OutboundMessage(ORIGIN, "done", on_delivery=receipt))
    task = asyncio.create_task(manager._dispatch_outbound())
    try:
        for _ in range(100):
            if receipt.await_count:
                break
            await asyncio.sleep(0.001)
        receipt.assert_awaited_once_with(return_value is True)
    finally:
        task.cancel()
        await task


def test_runtime_extension_default_is_empty_and_config_does_not_import_vendor(tmp_path):
    config = Config(storage_workspace=str(tmp_path))
    context = RuntimeContext(config, tmp_path, MessageBus(), ToolRegistry(config=config),
                             SessionManager(config.bot_data_path), None)
    assert RuntimeExtensions(context).extensions == []


async def test_real_agent_loop_delivers_internal_completion_without_calling_provider(setup, monkeypatch):
    from vikingbot.agent.loop import AgentLoop

    service, context, sandbox = setup
    monkeypatch.setattr(AgentLoop, "_register_builtin_hooks", lambda self: None)
    monkeypatch.setattr(AgentLoop, "_register_default_tools", lambda self: None)
    monkeypatch.setattr("vikingbot.agent.loop.SubagentManager", lambda **kw: None)
    provider = SimpleNamespace(get_default_model=lambda: "fake", chat=AsyncMock())
    loop = AgentLoop(service.host.bus, provider, service.host.workspace, config=service.host.config,
                     session_manager=service.host.sessions, sandbox_manager=service.host.sandbox_manager)
    item = await watch(service, context)
    sandbox.state = "succeeded"
    await service.tick(item)
    task = asyncio.create_task(loop.run())
    try:
        outbound = await asyncio.wait_for(service.host.bus.consume_outbound(), 2)
        assert "修复完成" in outbound.content
        provider.chat.assert_not_awaited()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_internal_completion_waits_behind_busy_turn_and_cannot_steer(setup, monkeypatch):
    from vikingbot.agent.loop import AgentLoop

    service, context, sandbox = setup
    monkeypatch.setattr(AgentLoop, "_register_builtin_hooks", lambda self: None)
    monkeypatch.setattr(AgentLoop, "_register_default_tools", lambda self: None)
    monkeypatch.setattr("vikingbot.agent.loop.SubagentManager", lambda **kw: None)
    loop = AgentLoop(service.host.bus, SimpleNamespace(get_default_model=lambda: "fake"),
                     service.host.workspace, config=service.host.config, session_manager=service.host.sessions)
    entered, release = asyncio.Event(), asyncio.Event()

    async def busy(msg, active_turn=None):
        entered.set()
        await release.wait()

    monkeypatch.setattr(loop, "_process_message", busy)
    await service.host.bus.publish_inbound(InboundMessage("user", "busy", ORIGIN))
    task = asyncio.create_task(loop.run())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        item = await watch(service, context)
        sandbox.state = "succeeded"
        await service.tick(item)
        event = service.host.bus.inbound._queue[0]
        assert not loop.steer(event)
        assert service.host.bus.count_pending_inbound_session(ORIGIN) == 0
        assert service.host.bus.outbound.qsize() == 0
        release.set()
        msg = await asyncio.wait_for(service.host.bus.consume_outbound(), 2)
        assert "修复完成" in msg.content
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_live_extension_uses_real_sandbox_and_preserves_large_json_result(tmp_path, monkeypatch):
    """End-to-end lifecycle/transport test with a real local CLI, no model calls.

    The fake CLI models an asynchronous runner whose launcher has returned while
    status is still running. Its final JSON exceeds DirectBackend's terminal cap.
    """
    from vikingbot.agent.loop import AgentLoop
    from vikingbot.sandbox.backends.direct import DirectBackend

    config = Config(storage_workspace=str(tmp_path), runtime_extensions=[{
        "factory": "vikingbot.extensions.background_tasks:create_extension",
        "options": {"poll_interval": 0.05},
    }])
    config.sandbox.backends.direct.allow_exec = True
    workspace = tmp_path / "sandbox $literal"
    workspace.mkdir()
    state_path = workspace / "fake-state.json"
    state_path.write_text(json.dumps({"state": "running"}))
    cli = workspace / "runner ' literal.py"
    cli.write_text(
        "import codecs,hashlib,json,pathlib,sys\n"
        "state=json.loads(pathlib.Path(sys.argv[1]).read_text())['state']\n"
        f"value={{'task_id':{TASK!r},'run_id':{RUN!r},'state':state,'protocol_version':2}}\n"
        "if sys.argv[2]=='result':\n"
        "    text='验证证据。'*3000\n"
        "    data=text.encode('utf-8')\n"
        "    offset=int(sys.argv[sys.argv.index('--offset')+1])\n"
        "    chunk=codecs.getincrementaldecoder('utf-8')().decode(data[offset:offset+30000],final=False)\n"
        "    end=offset+len(chunk.encode('utf-8'))\n"
        "    value.update(ready=True,text=chunk,offset=offset,next_offset=end,eof=end==len(data),\n"
        "                 total_bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),\n"
        "                 outcome_status='completed',outcome_error=None,source_truncated=False)\n"
        "print(json.dumps(value,ensure_ascii=False))\n"
    )
    backend = DirectBackend(config.sandbox, "watcher-test", workspace)
    await backend.start()
    sandbox_manager = SimpleNamespace(get_sandbox=AsyncMock(return_value=backend),
                                      get_workspace_path=lambda key: workspace,
                                      to_workspace_id=lambda key: "watcher-test")
    monkeypatch.setattr(AgentLoop, "_register_builtin_hooks", lambda self: None)
    monkeypatch.setattr(AgentLoop, "_register_default_tools", lambda self: None)
    monkeypatch.setattr("vikingbot.agent.loop.SubagentManager", lambda **kw: None)
    provider = SimpleNamespace(get_default_model=lambda: "fake", chat=AsyncMock())
    bus = MessageBus()
    loop = AgentLoop(bus, provider, tmp_path / "host", config=config, sandbox_manager=sandbox_manager)
    channel_manager = ChannelManager(bus)
    send = AsyncMock(return_value=True)
    channel_manager.channels[ORIGIN.channel_key()] = SimpleNamespace(send=send)
    dispatch = asyncio.create_task(channel_manager._dispatch_outbound())
    run = asyncio.create_task(loop.run())
    try:
        service = loop.runtime_extensions.extensions[0]
        for _ in range(200):
            if service.running:
                break
            await asyncio.sleep(0.01)
        context = ToolContext(session_key=ORIGIN, sender_id="user", sandbox_manager=sandbox_manager,
                              channel_metadata={"reply_to": "original-chat", "root_id": "original-root"})
        result = await loop.tools.get("background_task").execute(
            context, "watch", task_id=TASK, runner=[sys.executable, str(cli), str(state_path)],
        )
        item = service.items[json.loads(result)["id"]]
        assert send.await_count == 0
        state_path.write_text(json.dumps({"state": "succeeded"}))
        for _ in range(400):
            if item["delivery"] == "sent":
                break
            await asyncio.sleep(0.01)
        assert item["delivery"] == "sent"
        assert send.await_count > 1
        messages = [call.args[0] for call in send.await_args_list]
        message = messages[0]
        assert "".join(m.content.split("\n\n", 1)[1] for m in messages) == "验证证据。" * 3000
        assert service._parts_dir(item).joinpath("source.txt").read_text() == "验证证据。" * 3000
        assert item["next_part"] == item["part_count"] == len(messages)
        assert message.session_key == ORIGIN
        provider.chat.assert_not_awaited()
        probes = list(workspace.glob("tmp/wangoon/background_tasks/probes/*/result.json"))
        assert len(probes) == 1 and probes[0].stat().st_size <= 65536
        assert stat.S_IMODE(probes[0].stat().st_mode) == 0o600
    finally:
        run.cancel()
        dispatch.cancel()
        await asyncio.gather(run, dispatch, return_exceptions=True)
        await loop.close_mcp()
        await backend.stop()


async def test_delivery_budget_survives_restart_without_unlimited_extra_attempts(setup):
    service, context, sandbox = setup
    item = await watch(service, context)
    sandbox.state = "succeeded"
    await deliver(service, item)
    item["attempts"] = service.max_attempts
    service._save(item)
    await service.close()
    await service.start()
    service.worker.cancel()
    await asyncio.gather(service.worker, return_exceptions=True)
    service.worker = None
    recovered = service.items[item["id"]]
    await service.tick(recovered)
    assert recovered["delivery"] == "failed"
    assert service.host.bus.inbound_size == 0


async def test_history_save_failure_is_retried_and_persisted_before_delivery(setup, monkeypatch):
    service, context, sandbox = setup
    item = await watch(service, context)
    sandbox.state = "succeeded"
    original_save = service.host.sessions.save
    calls = 0

    async def flaky_save(session):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temporary history write failure")
        await original_save(session)

    monkeypatch.setattr(service.host.sessions, "save", flaky_save)
    await service.tick(item)
    event = await service.host.bus.consume_inbound()
    await event.internal_handler()
    assert service.host.bus.outbound.qsize() == 0 and not item["history_recorded"]
    item["next_delivery"] = 0
    msg = await deliver(service, item)
    await msg.on_delivery(True)
    reloaded = SessionManager(service.host.config.bot_data_path).get_or_create(ORIGIN)
    assert len(reloaded.messages) == 1
    assert reloaded.messages[0]["content"] == msg.content


async def test_failed_probe_command_cannot_reuse_stale_json(setup):
    service, context, sandbox = setup
    item = await watch(service, context)
    await service.tick(item)  # a valid running status file now exists
    sandbox.command_failed = True
    await service.tick(item)
    msg = await deliver(service, item)
    assert item["paused"] and "暂停自动监测" in msg.content


async def test_stop_intent_disables_old_continuation_even_when_command_fails(setup):
    service, context, sandbox = setup
    item = await watch(service, context)
    tool = service.host.tools.get("background_task")
    await tool.execute(context, "enqueue", watch_id=item["id"], text="old target addition")
    sandbox.command_failed = True
    result = await tool.execute(context, "stop", watch_id=item["id"])
    assert result.startswith("Error:")
    assert item["continuation"] == "cancelled"


async def test_stale_watch_stop_cannot_signal_a_newer_run(setup):
    service, context, sandbox = setup
    item = await watch(service, context)
    sandbox.run = "c" * 32
    result = await service.host.tools.get("background_task").execute(context, "stop", watch_id=item["id"])
    assert result.startswith("Error:")
    assert sandbox.state == "running"
    assert sandbox.calls[-1][0][-2:] == ["--expected-run-id", RUN]
    assert item["continuation"] == "cancelled" and item["generation"] == 1


@pytest.mark.parametrize("version", [0, 1])
async def test_unsafe_older_runner_protocol_is_rejected(setup, version):
    service, context, sandbox = setup
    sandbox.protocol_version = version
    with pytest.raises(ValueError, match="protocol v2"):
        await watch(service, context)
    assert not service.items


def _agent_loop(service, monkeypatch):
    from vikingbot.agent.loop import AgentLoop

    monkeypatch.setattr(AgentLoop, "_register_builtin_hooks", lambda self: None)
    monkeypatch.setattr(AgentLoop, "_register_default_tools", lambda self: None)
    monkeypatch.setattr("vikingbot.agent.loop.SubagentManager", lambda **kw: None)
    return AgentLoop(service.host.bus, SimpleNamespace(get_default_model=lambda: "fake", chat=AsyncMock()),
                     service.host.workspace, config=service.host.config,
                     session_manager=service.host.sessions, sandbox_manager=service.host.sandbox_manager)


async def _queued_continuation(service, context, sandbox):
    item = await watch(service, context)
    await service.host.tools.get("background_task").execute(context, "enqueue", watch_id=item["id"], text="旧范围补充")
    sandbox.state = "succeeded"
    msg = await deliver(service, item)
    await msg.on_delivery(True)
    await service.tick(item)
    return item, await service.host.bus.consume_inbound()


@pytest.mark.parametrize("stop_fails", [False, True])
async def test_queued_continuation_is_invalidated_at_execution_time(setup, monkeypatch, stop_fails):
    service, context, sandbox = setup
    item, continuation = await _queued_continuation(service, context, sandbox)
    loop = _agent_loop(service, monkeypatch)
    process = AsyncMock()
    monkeypatch.setattr(loop, "_process_message", process)
    assert await continuation.before_process()
    sandbox.command_failed = stop_fails
    await service.host.tools.get("background_task").execute(context, "stop", watch_id=item["id"])
    assert await loop._process_message_as_active_turn(continuation) is None
    process.assert_not_awaited()
    assert item["queued"] and item["continuation"] == "cancelled"


async def test_internal_continuation_does_not_extend_upstream_interrupt_behavior(setup, monkeypatch):
    service, context, sandbox = setup
    item, continuation = await _queued_continuation(service, context, sandbox)
    loop = _agent_loop(service, monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def busy(msg, active_turn=None):
        entered.set()
        await release.wait()

    monkeypatch.setattr(loop, "_process_message", busy)
    task = asyncio.create_task(loop._process_message_as_active_turn(continuation))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert not loop.steer(InboundMessage("user", "new target", ORIGIN))
        assert not await loop.interrupt(ORIGIN)
        assert not task.done()
        assert item["continuation"] == "dispatched" and item["generation"] == 0
        assert await continuation.before_process()
        release.set()
        await asyncio.wait_for(task, 1)
        # The extension adds no conversation-wide cancellation or process stop.
        assert not any("stop" in argv for argv, _ in sandbox.calls)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_large_unicode_result_is_fetched_completely_and_delivered_in_bounded_parts(setup):
    from vikingbot.extensions.background_tasks.segments import encoded_size

    service, context, sandbox = setup
    source = '日志🙂 "quote" \\path\n' * 26000
    assert len(json.dumps({"answer": source, "outcome": source}, ensure_ascii=False).encode()) > 1024 * 1024
    sandbox.outcome["text"] = source
    item = await watch(service, context)
    sandbox.state = "succeeded"
    messages = []
    while item["delivery"] != "sent":
        msg = await deliver(service, item)
        messages.append(msg)
        assert encoded_size(msg.content) <= service.segment_bytes
        assert msg.session_key == ORIGIN and msg.metadata["root_id"] == "original-root"
        await msg.on_delivery(True)
    assert not item["paused"] and len(messages) > 100
    assert service._parts_dir(item).joinpath("source.txt").read_text() == source
    assert item["next_part"] == item["part_count"] == len(messages)
    assert len(service.host.sessions.get_or_create(ORIGIN).messages) == 1
    journal = service.directory / (item["id"] + ".json")
    assert journal.stat().st_size < 100000
    assert stat.S_IMODE(service._parts_dir(item).joinpath("source.txt").stat().st_mode) == 0o600
    assert all("第 " in message.content for message in messages)
    assert "".join(m.content.split("\n\n", 1)[1] for m in messages) == source
    assert stat.S_IMODE(service._parts_dir(item).parent.stat().st_mode) == 0o700
    assert len([1 for argv, _ in sandbox.calls if "result" in argv]) > 2


@pytest.mark.parametrize("corruption", ["cursor", "checksum", "run", "manifest", "limit"])
async def test_corrupt_paging_never_delivers_unverified_text(setup, corruption):
    service, context, sandbox = setup
    service.max_errors = 1
    sandbox.outcome["text"] = "不应发送的原文" * 2000
    item = await watch(service, context)
    sandbox.state = "succeeded"

    def mutate(page):
        if corruption == "cursor":
            page["next_offset"] += 1
        elif corruption == "checksum":
            page["sha256"] = "0" * 64
        elif corruption == "run":
            page["run_id"] = "c" * 32
        elif corruption == "manifest" and page["offset"] > 0:
            page["outcome_status"] = "blocked"
        elif corruption == "limit":
            page["total_bytes"] = 64 * 1024 * 1024 + 1

    sandbox.mutate_page = mutate
    msg = await deliver(service, item)
    assert item["paused"] and "任务已结束" in msg.content
    assert "可能仍在运行" not in msg.content and "不应发送的原文" not in msg.content


async def test_multipart_retry_restart_and_late_receipt_preserve_confirmed_cursor(setup):
    service, context, sandbox = setup
    sandbox.outcome["text"] = "证据与修复细节\n" * 600
    item = await watch(service, context)
    tool = service.host.tools.get("background_task")
    await tool.execute(context, "enqueue", watch_id=item["id"], text="补充测试")
    sandbox.state = "succeeded"
    first = await deliver(service, item)
    await first.on_delivery(True)
    second = await deliver(service, item)
    assert item["next_part"] == 1 and item["part_count"] > 2
    await second.on_delivery(False)
    item["next_delivery"] = 0
    failed_retry = await deliver(service, item)
    assert failed_retry.content == second.content
    await failed_retry.on_delivery(False)
    assert item["delivery"] == "failed" and item["continuation"] == "pending"
    await tool.execute(context, "retry", watch_id=item["id"])
    await service.close()
    await service.start()
    service.worker.cancel()
    await asyncio.gather(service.worker, return_exceptions=True)
    service.worker = None
    recovered = service.items[item["id"]]
    assert recovered["next_part"] == 1
    resumed = await deliver(service, recovered)
    assert resumed.content == second.content
    await first.on_delivery(True)  # receipt from old service incarnation
    await failed_retry.on_delivery(True)
    assert recovered["next_part"] == 1
    await resumed.on_delivery(True)
    assert recovered["next_part"] == 2 and recovered["attempts"] == 0
    while recovered["delivery"] != "sent":
        assert recovered["continuation"] == "pending"
        msg = await deliver(service, recovered)
        assert msg.content != first.content
        await msg.on_delivery(True)
    await service.tick(recovered)
    followup = await service.host.bus.consume_inbound()
    assert followup.before_process and "--expected-run-id " + RUN in followup.content
    assert len(service.host.sessions.get_or_create(ORIGIN).messages) == 1
    # An explicit retry after success never re-sends the whole answer.
    await tool.execute(context, "retry", watch_id=recovered["id"])
    await service.tick(recovered)
    assert service.host.bus.inbound_size == 0


async def test_old_single_message_journal_migrates_without_resending_confirmed_result(setup):
    service, context, sandbox = setup
    item = await watch(service, context)
    sandbox.state = "succeeded"
    msg = await deliver(service, item)
    await msg.on_delivery(True)
    for key in ("generation", "part_count", "next_part", "delivery_epoch", "watch_error"):
        item.pop(key)
    service._save(item)
    await service.close()
    await service.start()
    service.worker.cancel()
    await asyncio.gather(service.worker, return_exceptions=True)
    service.worker = None
    recovered = service.items[item["id"]]
    await service.tick(recovered)
    assert recovered["delivery"] == "sent" and recovered["next_part"] == recovered["part_count"] == 1
    assert service.host.bus.inbound_size == 0
    assert len(service.host.sessions.get_or_create(ORIGIN).messages) == 1


async def test_receipt_wakes_next_part_without_waiting_for_long_poll_interval(setup):
    service, context, sandbox = setup
    sandbox.outcome["text"] = "完整回复\n" * 600
    item = await watch(service, context)
    sandbox.state = "succeeded"
    first = await deliver(service, item)
    service.worker = asyncio.create_task(service._watch_loop())
    try:
        await first.on_delivery(True)
        event = await asyncio.wait_for(service.host.bus.consume_inbound(), 1)
        await event.internal_handler()
        second = await asyncio.wait_for(service.host.bus.consume_outbound(), 1)
        assert second.content != first.content and "第 2/" in second.content
    finally:
        service.worker.cancel()
        await asyncio.gather(service.worker, return_exceptions=True)
        service.worker = None


async def test_late_receipt_from_expired_attempt_cannot_confirm_retry_or_next_part(setup):
    service, context, sandbox = setup
    sandbox.outcome["text"] = "内容\n" * 1000
    item = await watch(service, context)
    sandbox.state = "succeeded"
    expired = await deliver(service, item)
    item["receipt_deadline"] = time.time() - 1
    retry = await deliver(service, item)
    assert expired.content == retry.content
    await expired.on_delivery(True)
    assert item["next_part"] == 0 and item["attempts"] == 2
    await retry.on_delivery(True)
    assert item["next_part"] == 1 and item["attempts"] == 0
    second = await deliver(service, item)
    await retry.on_delivery(True)
    assert item["next_part"] == 1
    await second.on_delivery(True)
    assert item["next_part"] == 2


@pytest.mark.parametrize("success", [True, False])
async def test_receipt_journal_failure_keeps_cursor_until_commit_then_recovers(setup, monkeypatch, success):
    service, context, sandbox = setup
    sandbox.outcome["text"] = "完整答复\n" * 1000
    item = await watch(service, context)
    sandbox.state = "succeeded"
    msg = await deliver(service, item)
    before = copy.deepcopy(item)
    original_save = service._save
    failures = 1

    def flaky_save(value):
        nonlocal failures
        if failures:
            failures -= 1
            raise OSError("temporary receipt journal failure")
        original_save(value)

    monkeypatch.setattr(service, "_save", flaky_save)
    await msg.on_delivery(success)
    assert item == before  # a memory-only acknowledgement must not skip a part
    await msg.on_delivery(success)  # duplicate callback cannot reset the retry budget
    assert item == before
    journal = json.loads((service.directory / (item["id"] + ".json")).read_text())
    assert journal["next_part"] == 0 and journal["delivery_epoch"] == before["delivery_epoch"]
    # A pending receipt must be persisted before considering a send timeout.
    item["receipt_deadline"] = time.time() - 1
    await service.tick(item)
    assert service.host.bus.inbound_size == 0
    await asyncio.sleep(service.retry_interval * 1.1)
    await service.tick(item)
    assert item["next_part"] == (1 if success else 0)
    assert item["id"] not in service.inflight
    assert item["receipt_deadline"] == 0
    if not success:
        item["next_delivery"] = 0
    next_message = await deliver(service, item)
    assert (next_message.content != msg.content) is success
    assert len(service.host.sessions.get_or_create(ORIGIN).messages) == 1


async def test_receipt_journal_failures_stop_at_budget_and_allow_manual_retry(setup, monkeypatch):
    service, context, sandbox = setup
    item = await watch(service, context)
    sandbox.state = "succeeded"
    msg = await deliver(service, item)
    original_save = service._save
    writes = 0

    def unavailable(value):
        nonlocal writes
        writes += 1
        raise OSError("storage unavailable")

    monkeypatch.setattr(service, "_save", unavailable)
    await msg.on_delivery(True)
    for _ in range(service.max_attempts + 1):
        await asyncio.sleep(service.retry_interval * 1.1)
        await service.tick(item)
    assert writes == service.max_attempts
    assert item["delivery"] == "failed" and item["next_part"] == 0
    assert item["id"] not in service.inflight and not item["receipt_deadline"]
    assert service.host.bus.inbound_size == 0
    view = json.loads(await service.host.tools.get("background_task").execute(context, "list"))[0]
    assert view["delivery_error"] == "receipt_journal_failed"
    unavailable_retry = await service.host.tools.get("background_task").execute(context, "retry", watch_id=item["id"])
    assert unavailable_retry.startswith("Error:") and item["delivery"] == "failed"
    await service.tick(item)
    assert service.host.bus.inbound_size == 0
    monkeypatch.setattr(service, "_save", original_save)
    result = await service.host.tools.get("background_task").execute(context, "retry", watch_id=item["id"])
    assert not result.startswith("Error:")
    retry = await deliver(service, item)
    assert retry.content == msg.content  # only the uncommitted part may repeat
    await retry.on_delivery(True)
    assert item["delivery"] == "sent" and item["next_part"] == 1


async def test_restart_after_receipt_write_failure_retains_last_durable_cursor(setup, monkeypatch):
    service, context, sandbox = setup
    sandbox.outcome["text"] = "分段原文\n" * 1200
    item = await watch(service, context)
    sandbox.state = "succeeded"
    first = await deliver(service, item)
    await first.on_delivery(True)
    second = await deliver(service, item)
    original_save = service._save

    def unavailable(value):
        raise OSError("storage unavailable before restart")

    monkeypatch.setattr(service, "_save", unavailable)
    await second.on_delivery(True)
    await service.close()
    monkeypatch.setattr(service, "_save", original_save)
    await service.start()
    service.worker.cancel()
    await asyncio.gather(service.worker, return_exceptions=True)
    service.worker = None
    recovered = service.items[item["id"]]
    assert recovered["next_part"] == 1
    await second.on_delivery(True)  # a callback from the previous incarnation
    assert recovered["next_part"] == 1
    retry = await deliver(service, recovered)
    assert retry.content == second.content and retry.content != first.content
    await retry.on_delivery(True)
    assert recovered["next_part"] == 2


async def test_pending_receipt_commit_preserves_cancellation_written_in_between(setup, monkeypatch):
    service, context, sandbox = setup
    item = await watch(service, context)
    tool = service.host.tools.get("background_task")
    await tool.execute(context, "enqueue", watch_id=item["id"], text="旧补充")
    sandbox.state = "succeeded"
    msg = await deliver(service, item)
    original_save = service._save

    def unavailable(value):
        raise OSError("temporarily unavailable")

    monkeypatch.setattr(service, "_save", unavailable)
    await msg.on_delivery(True)
    monkeypatch.setattr(service, "_save", original_save)
    await tool.execute(context, "stop", watch_id=item["id"])
    await asyncio.sleep(service.retry_interval * 1.1)
    await service.tick(item)
    await service.tick(item)
    assert item["delivery"] == "sent" and item["continuation"] == "cancelled"
    assert item["generation"] == 1 and service.host.bus.inbound_size == 0


async def test_failed_watch_does_not_leave_a_memory_only_registration(setup, monkeypatch):
    service, context, _ = setup
    tool = service.host.tools.get("background_task")
    save = service._save

    def unavailable(value):
        raise OSError("journal temporarily unavailable")

    monkeypatch.setattr(service, "_save", unavailable)
    result = await tool.execute(context, "watch", task_id=TASK, runner=["python3", "/runner.py"])
    assert result.startswith("Error:")
    assert not service.items and not list(service.directory.glob("*.json"))
    monkeypatch.setattr(service, "_save", save)
    registered = json.loads(await tool.execute(context, "watch", task_id=TASK, runner=["python3", "/runner.py"]))
    journal = json.loads((service.directory / (registered["id"] + ".json")).read_text())
    assert journal["id"] == registered["id"]
    await service.close()
    await service.start()
    assert registered["id"] in service.items


@pytest.mark.parametrize("action", ["enqueue", "ack"])
async def test_failed_queue_change_preserves_live_and_saved_state_until_retry(setup, monkeypatch, action):
    service, context, _ = setup
    item = await watch(service, context)
    tool = service.host.tools.get("background_task")
    if action == "ack":
        item.update(queued=[{"text": "existing addition", "sender_id": context.sender_id}],
                    continuation="dispatched")
        service._save(item)
    before = copy.deepcopy(item)
    save = service._save

    def unavailable(value):
        raise OSError("journal temporarily unavailable")

    kwargs = {"text": "one addition"} if action == "enqueue" else {}
    monkeypatch.setattr(service, "_save", unavailable)
    result = await tool.execute(context, action, watch_id=item["id"], **kwargs)
    assert result.startswith("Error:") and item == before
    assert json.loads((service.directory / (item["id"] + ".json")).read_text()) == before
    monkeypatch.setattr(service, "_save", save)
    result = json.loads(await tool.execute(context, action, watch_id=item["id"], **kwargs))
    assert len(result["queued"]) == (1 if action == "enqueue" else 0)
    assert result["continuation"] == ("pending" if action == "enqueue" else "acknowledged")
    await service.close()
    await service.start()
    assert service.items[item["id"]] == item


@pytest.mark.parametrize("failure_marker_persists", [True, False])
async def test_watch_write_failure_is_isolated_and_recovers_without_replaying_result(
    setup, monkeypatch, failure_marker_persists,
):
    service, context, sandbox = setup
    item = await watch(service, context)
    tool = service.host.tools.get("background_task")
    await tool.execute(context, "enqueue", watch_id=item["id"], text="follow-up requirement")
    sandbox.state = "succeeded"
    result = await deliver(service, item)
    await result.on_delivery(True)
    # A second watch must continue delivering while the first cannot persist
    # its continuation. This exercises the live supervisor, not manual ticks.
    healthy = copy.deepcopy(item)
    healthy.update(id="d" * 64, task_id="e" * 32, delivery="pending", attempts=0,
                   next_part=0, part_count=0, text=None, queued=[], history_recorded=False)
    service._store_text(healthy, "independent result")
    service.items[healthy["id"]] = healthy
    save = service._save
    broken = True
    paused = asyncio.Event()
    failed_dispatches = 0

    def faulty_save(value):
        nonlocal failed_dispatches
        if value["id"] == item["id"] and broken:
            if value.get("watch_error"):
                paused.set()
            if value["continuation"] == "dispatched":
                failed_dispatches += 1
                raise OSError("continuation write failed")
            if not failure_marker_persists:
                raise OSError("watch journal unavailable")
        save(value)

    monkeypatch.setattr(service, "_save", faulty_save)
    service.worker = asyncio.create_task(service._watch_loop())
    await asyncio.wait_for(paused.wait(), 1)
    event = await asyncio.wait_for(service.host.bus.consume_inbound(), 1)
    assert event.internal_handler  # the failed continuation was never enqueued
    await event.internal_handler()
    delivered = await asyncio.wait_for(service.host.bus.consume_outbound(), 1)
    assert "independent result" in delivered.content
    await delivered.on_delivery(True)
    assert healthy["delivery"] == "sent"
    assert service.running and not service.worker.done()
    assert item["continuation"] == "pending" and item["watch_error"] == "watch_failed"
    listed = json.loads(await tool.execute(context, "list"))
    assert next(w for w in listed if w["id"] == item["id"])["watch_error"] == "watch_failed"
    saved = json.loads((service.directory / (item["id"] + ".json")).read_text())
    assert bool(saved.get("watch_error")) is failure_marker_persists
    await asyncio.sleep(0.05)
    assert failed_dispatches == 1  # no supervisor restart loop or automatic replay
    if not failure_marker_persists:
        retry = await tool.execute(context, "retry", watch_id=item["id"])
        assert retry.startswith("Error:") and item["watch_error"] == "watch_failed"
    broken = False
    recovered = json.loads(await tool.execute(context, "retry", watch_id=item["id"]))
    assert recovered["watch_error"] is None and recovered["delivery"] == "sent"
    continuation = await asyncio.wait_for(service.host.bus.consume_inbound(), 1)
    assert continuation.internal_handler is None and await continuation.before_process()
    assert "follow-up requirement" in continuation.content
    assert not service.host.bus.inbound_size and service.host.bus.outbound.empty()
    assert item["continuation"] == "dispatched" and item["queued"]


async def test_monitor_retry_preserves_an_outstanding_delivery_receipt(setup):
    service, context, sandbox = setup
    item = await watch(service, context)
    sandbox.state = "succeeded"
    message = await deliver(service, item)
    epoch, attempts = item["delivery_epoch"], item["attempts"]
    service._pause_watch(item, OSError("isolated watch fault"))
    result = json.loads(await service.host.tools.get("background_task").execute(
        context, "retry", watch_id=item["id"],
    ))
    assert result["watch_error"] is None and item["id"] in service.inflight
    assert item["delivery_epoch"] == epoch and item["attempts"] == attempts
    await message.on_delivery(True)
    assert item["delivery"] == "sent" and item["next_part"] == item["part_count"]
    await service.tick(item)
    assert service.host.bus.inbound_size == 0


async def test_saved_watch_failure_stays_paused_across_restart_until_retry(setup):
    service, context, sandbox = setup
    item = await watch(service, context)
    service._pause_watch(item, OSError("isolated watch fault"))
    await service.close()
    await service.start()
    recovered = service.items[item["id"]]
    calls = len(sandbox.calls)
    sandbox.state = "succeeded"
    await asyncio.sleep(0.05)
    assert recovered["watch_error"] == "watch_failed" and len(sandbox.calls) == calls
    assert service.host.bus.inbound_size == 0 and service.running
    retry = json.loads(await service.host.tools.get("background_task").execute(
        context, "retry", watch_id=item["id"],
    ))
    assert retry["watch_error"] is None
    event = await asyncio.wait_for(service.host.bus.consume_inbound(), 1)
    await event.internal_handler()
    result = await asyncio.wait_for(service.host.bus.consume_outbound(), 1)
    await result.on_delivery(True)
    assert recovered["delivery"] == "sent"


async def test_delivery_preparation_write_failure_pauses_only_that_watch_until_retry(setup, monkeypatch):
    service, context, sandbox = setup
    item = await watch(service, context)
    sandbox.state = "succeeded"
    await service.tick(item)
    event = await service.host.bus.consume_inbound()
    save = service._save
    writes = 0

    def unavailable(value):
        nonlocal writes
        writes += 1
        raise OSError("journal unavailable during delivery preparation")

    service.worker = asyncio.create_task(service._watch_loop())
    monkeypatch.setattr(service, "_save", unavailable)
    await event.internal_handler()
    assert item["watch_error"] == "watch_failed" and item["attempts"] == 0
    assert item["next_part"] == 0 and item["id"] not in service.inflight
    await asyncio.sleep(0.05)
    assert writes == 3 and service.running and not service.worker.done()
    assert service.host.bus.outbound.empty()
    monkeypatch.setattr(service, "_save", save)
    retry = json.loads(await service.host.tools.get("background_task").execute(
        context, "retry", watch_id=item["id"],
    ))
    assert retry["watch_error"] is None
    recovered_event = await asyncio.wait_for(service.host.bus.consume_inbound(), 1)
    await recovered_event.internal_handler()
    result = await asyncio.wait_for(service.host.bus.consume_outbound(), 1)
    await result.on_delivery(True)
    assert item["delivery"] == "sent" and item["next_part"] == item["part_count"]


@pytest.mark.parametrize("slow_action", ["status", "result"])
async def test_slow_probe_does_not_block_other_watch_parts_or_overpoll_running_tasks(setup, monkeypatch, slow_action):
    service, context, sandbox = setup
    sandbox.outcome["text"] = "完整分段内容\n" * 1000
    item = await watch(service, context)
    sandbox.state = "succeeded"
    slow, healthy = copy.deepcopy(item), copy.deepcopy(item)
    slow.update(id="d" * 64, task_id="e" * 32)
    healthy.update(id="f" * 64, task_id="0" * 32)
    service.items.update({slow["id"]: slow, healthy["id"]: healthy})
    original_call = service._call
    entered, cancelled, healthy_probed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = 0

    async def call(value, action, offset=0):
        nonlocal calls
        if value["id"] == slow["id"]:
            if action == slow_action:
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()
            return {"state": "succeeded"}
        if value["id"] == healthy["id"]:
            calls += 1
            healthy_probed.set()
            return {"state": "running"}
        return await original_call(value, action, offset)

    monkeypatch.setattr(service, "_call", call)
    service.worker = asyncio.create_task(service._watch_loop())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(healthy_probed.wait(), 1)
        messages = []
        while item["delivery"] != "sent":
            event = await asyncio.wait_for(service.host.bus.consume_inbound(), 1)
            await event.internal_handler()
            message = await asyncio.wait_for(service.host.bus.consume_outbound(), 1)
            messages.append(message.content)
            await message.on_delivery(True)
        assert len(messages) > 2
        assert "".join(part.split("\n\n", 1)[1] for part in messages) == sandbox.outcome["text"]
        assert calls == 1  # per-part wakeups must not trigger other status queries
        assert not cancelled.is_set()
    finally:
        await service.close()
    assert cancelled.is_set() and not service.inflight
