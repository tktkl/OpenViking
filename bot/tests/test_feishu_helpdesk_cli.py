"""lark-cli execution and auth-mcp token handoff without live Feishu calls."""

import asyncio
import json
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from loguru import logger

from vikingbot.extensions.feishu_helpdesk.cli import LarkCLIAdapter, LarkCLIError
from vikingbot.extensions.feishu_helpdesk.config import LarkCLIConfig


def auth_access_value(*, token="user-token", identity="ou_agent", expires_at=None, state="ready"):
    value = {
        "state": state,
        "identity": identity,
        "access": {"headers": {"Authorization": f"Bearer {token}"}},
    }
    if expires_at is not None:
        value["expires_at"] = expires_at
    return value


def success(data=None):
    return {"ok": True, "identity": "user", "data": data if data is not None else {}}


@pytest.fixture
def cli():
    adapter = LarkCLIAdapter(LarkCLIConfig(), "cli_bot")
    adapter._auth_access = AsyncMock(return_value=auth_access_value())
    adapter._command = AsyncMock(return_value=success())
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("created", ["1800000000123", 1800000000123])
async def test_followup_send_preserves_raw_timestamp_and_user_idempotency(cli, created):
    cli._command.side_effect = lambda *args, **kwargs: success(
        {"message_id": "om_sent", "create_time": created}
    )
    before = AsyncMock(return_value=None)
    receipt = await cli.send_text("oc_ticket", "提醒", "stable-uuid", before_call=before)
    before.assert_awaited_once_with("ou_agent")
    assert receipt["message_id"] == "om_sent"
    assert receipt["create_time"] == created
    args = cli._command.call_args.args
    assert args[:4] == ("api", "POST", "/open-apis/im/v1/messages", "--params")
    assert json.loads(args[4]) == {"receive_id_type": "chat_id"}
    assert args[5] == "--data"
    body = json.loads(args[6])
    assert body == {
        "receive_id": "oc_ticket",
        "msg_type": "text",
        "content": json.dumps({"text": "提醒"}, ensure_ascii=False),
        "uuid": "stable-uuid",
    }
    assert args[-4:] == ("--as", "user", "--format", "json")
    assert cli._command.call_args.kwargs["auth"]["access_token"] == "user-token"


@pytest.mark.asyncio
async def test_close_uses_auth_mcp_token_and_one_put(cli):
    before = AsyncMock(return_value=10)
    result = await cli.close_ticket(
        "t1", "private-desk-header", {"status": 51, "solved": 1}, before_call=before
    )
    assert result == {}
    before.assert_awaited_once_with("ou_agent")
    args = cli._command.call_args.args
    assert args[:3] == ("api", "PUT", "/open-apis/helpdesk/v1/tickets/t1")
    assert json.loads(args[args.index("--data") + 1]) == {"status": 51, "solved": 1}
    assert cli._command.await_count == 1


@pytest.mark.asyncio
async def test_receipt_reconciliation_reads_raw_user_history_with_seconds_and_pagination(cli):
    cli._command.return_value = success({"items": [], "has_more": False})
    await cli.chat_messages("oc_ticket", 1800000000.123, 1800000060.456, "next")
    args = cli._command.call_args.args
    assert args[:4] == ("api", "GET", "/open-apis/im/v1/messages", "--params")
    assert json.loads(args[4]) == {
        "container_id_type": "chat",
        "container_id": "oc_ticket",
        "start_time": "1800000000",
        "end_time": "1800000061",
        "sort_type": "ByCreateTimeAsc",
        "page_size": 50,
        "page_token": "next",
    }
    assert args[-4:] == ("--as", "user", "--format", "json")


@pytest.mark.asyncio
async def test_send_guard_runs_after_cli_lock_and_auth(cli):
    lock_entered, release = asyncio.Event(), asyncio.Event()

    async def holder():
        async with cli._lock:
            lock_entered.set()
            await release.wait()

    holding = asyncio.create_task(holder())
    await lock_entered.wait()
    before = AsyncMock(side_effect=RuntimeError("stale"))
    sending = asyncio.create_task(cli.send_text("oc_ticket", "旧提醒", "key", before_call=before))
    await asyncio.sleep(0)
    assert not before.called and not cli._command.called
    release.set()
    await holding
    with pytest.raises(RuntimeError, match="stale"):
        await sending
    assert cli._auth_access.await_count == 1
    assert cli._command.await_count == 0


@pytest.mark.asyncio
async def test_im_detail_requires_exact_native_message_id(cli):
    cli._command.return_value = success({"items": [{"message_id": "om_other", "chat_id": "oc_other"}]})
    with pytest.raises(LarkCLIError):
        await cli.message("om_expected")


@pytest.mark.asyncio
async def test_auth_mcp_not_ready_skips_user_command_and_logs_once(cli):
    cli._auth_access.return_value = auth_access_value(state="waiting_scan")
    messages = []
    sink = logger.add(lambda message: messages.append(str(message)))
    try:
        for _ in range(3):
            assert await cli.invite_bot("oc_ticket") is None
    finally:
        logger.remove(sink)
    assert cli._command.await_count == 0
    assert len(messages) == 1


@pytest.mark.asyncio
async def test_auth_mcp_transient_failure_is_retryable(cli):
    cli._auth_access.side_effect = LarkCLIError("auth MCP request failed", retryable=True)
    with pytest.raises(LarkCLIError) as failure:
        await cli.invite_bot("oc_ticket")
    assert failure.value.retryable
    assert cli._command.await_count == 0


@pytest.mark.asyncio
async def test_auth_mcp_token_is_cached_between_operations(cli):
    await cli.invite_bot("oc_first")
    await cli.invite_bot("oc_second")
    assert cli._auth_access.await_count == 1
    assert cli._command.await_count == 2


@pytest.mark.asyncio
async def test_near_expiry_token_is_replaced_before_call(cli):
    cli._auth_access.side_effect = [
        auth_access_value(token="expired", expires_at=time.time() - 1),
        auth_access_value(token="fresh", expires_at=time.time() + 3600),
    ]
    await cli.invite_bot("oc_ticket")
    assert cli._auth_access.await_count == 2
    assert cli._command.call_args.kwargs["auth"]["access_token"] == "fresh"


@pytest.mark.asyncio
async def test_cli_authentication_failure_refreshes_once_and_retries(cli):
    calls = 0

    def command(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise LarkCLIError("command failed", code=20037, authentication=True)
        return success({"ok": True})

    cli._command.side_effect = command
    await cli.invite_bot("oc_ticket")
    assert calls == 2
    assert cli._auth_access.await_count == 2


@pytest.mark.asyncio
async def test_invitation_uses_typed_command_and_explicit_user_identity(cli):
    assert await cli.invite_bot("oc_ticket") == {}
    args = cli._command.call_args.args
    assert args[:3] == ("im", "chat.members", "create")
    assert args[args.index("--chat-id") + 1] == "oc_ticket"
    assert args[args.index("--member-id-type") + 1] == "app_id"
    assert args[args.index("--succeed-type") + 1] == "2"
    assert json.loads(args[args.index("--data") + 1]) == {"id_list": ["cli_bot"]}
    assert args[-4:] == ("--as", "user", "--format", "json")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [{"code": 0, "data": {}}, {"ok": True, "identity": "bot", "data": {}}, success([])],
)
async def test_invalid_success_envelope_cannot_be_treated_as_user_invitation(cli, response):
    cli._command.return_value = response
    with pytest.raises(LarkCLIError, match="invalid user"):
        await cli.invite_bot("oc_ticket")


def test_keepalive_api_is_removed():
    cli = LarkCLIAdapter(LarkCLIConfig(), "cli_bot")
    assert not hasattr(cli, "keepalive_once")
    assert not hasattr(cli, "start")
    assert not hasattr(cli, "_keepalive_task")


@pytest.mark.asyncio
async def test_subprocess_uses_auth_mcp_token_and_suppresses_notifiers(monkeypatch):
    process = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(return_value=(json.dumps(success()).encode(), b"")),
    )
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    cli = LarkCLIAdapter(
        LarkCLIConfig(executable="/path with spaces/lark-cli", profile="support"), "cli_bot"
    )
    result = await cli._command(
        "api", "GET", "/open-apis/authen/v1/user_info",
        auth={"access_token": "external-token"},
    )
    assert result["ok"] is True
    assert spawn.call_args.args == (
        "/path with spaces/lark-cli",
        "--profile",
        "support",
        "api",
        "GET",
        "/open-apis/authen/v1/user_info",
    )
    assert spawn.call_args.kwargs["stdin"] == asyncio.subprocess.DEVNULL
    env = spawn.call_args.kwargs["env"]
    assert env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] == "1"
    assert env["LARKSUITE_CLI_APP_ID"] == "cli_bot"
    assert env["LARKSUITE_CLI_USER_ACCESS_TOKEN"] == "external-token"
    assert "LARKSUITE_CLI_APP_SECRET" not in env


@pytest.mark.asyncio
async def test_cli_error_parser_accepts_stderr_preamble_and_redacts_output(monkeypatch):
    failure = {
        "ok": False,
        "error": {
            "type": "authentication",
            "code": 20037,
            "message": "access_secret refresh_secret",
        },
    }
    process = SimpleNamespace(
        returncode=1,
        communicate=AsyncMock(
            return_value=(b"", b"[lark-cli] refreshing\n" + json.dumps(failure).encode())
        ),
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    cli = LarkCLIAdapter(LarkCLIConfig(), "cli_bot")
    with pytest.raises(LarkCLIError) as failure_info:
        await cli._command("api", "GET", "/open-apis/authen/v1/user_info")
    assert failure_info.value.authentication
    assert failure_info.value.code == 20037
    assert "secret" not in str(failure_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        ({"type": "network"}, True),
        ({"type": "api", "code": 99991400}, True),
        ({"type": "api", "retryable": True}, True),
        ({"type": "permission", "code": 232011}, False),
        ({"type": "permission", "code": 232017}, False),
        ({"type": "authentication", "code": 232011, "retryable": True}, False),
        ({"type": "authentication", "code": 20037, "retryable": True}, False),
    ],
)
async def test_cli_failure_metadata_controls_admission_retry(monkeypatch, error, retryable):
    process = SimpleNamespace(
        returncode=1,
        communicate=AsyncMock(
            return_value=(b"", json.dumps({"ok": False, "error": error}).encode())
        ),
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    cli = LarkCLIAdapter(LarkCLIConfig(), "cli_bot")
    with pytest.raises(LarkCLIError) as failure:
        await cli._command("im", "chat.members", "create")
    assert failure.value.retryable is retryable


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,rejected",
    [
        ({"type": "api", "code": 230035}, True),
        ({"type": "api", "code": 99991400, "retryable": True}, True),
        ({"type": "authorization", "code": 230027}, True),
        ({"type": "authentication", "code": 20037}, True),
        ({"type": "validation"}, True),
        ({"type": "network", "retryable": True}, False),
        ({"type": "policy"}, False),
        ({"type": "api", "code": 500}, False),
        ({"type": "internal"}, False),
        ({}, False),
    ],
)
async def test_only_known_cli_refusals_disprove_a_send(monkeypatch, error, rejected):
    process = SimpleNamespace(
        returncode=1,
        communicate=AsyncMock(
            return_value=(b"", json.dumps({"ok": False, "error": error}).encode())
        ),
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    cli = LarkCLIAdapter(LarkCLIConfig(), "cli_bot")
    with pytest.raises(LarkCLIError) as failure:
        await cli._command("api", "POST", "/open-apis/im/v1/messages")
    assert failure.value.rejected is rejected


@pytest.mark.asyncio
async def test_missing_cli_returns_fixed_error(monkeypatch):
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError))
    cli = LarkCLIAdapter(LarkCLIConfig(), "cli_bot")
    with pytest.raises(LarkCLIError, match="not installed"):
        await cli._command("api", "GET", "/open-apis/authen/v1/user_info")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancellation_reap_subprocess(monkeypatch, cancel):
    original_spawn = asyncio.create_subprocess_exec
    spawned = asyncio.Event()
    processes = []

    async def spawn(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        processes.append(process)
        spawned.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    cli = LarkCLIAdapter(LarkCLIConfig(executable=sys.executable, timeout_seconds=0.1), "cli_bot")
    task = asyncio.create_task(cli._command("-c", "import time; time.sleep(60)"))
    await asyncio.wait_for(spawned.wait(), timeout=1)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(LarkCLIError, match="timed out"):
            await task
    assert processes[0].returncode is not None
    if os.name == "posix":
        assert processes[0].returncode < 0
