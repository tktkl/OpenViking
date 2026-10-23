"""CLI identity gates, idle renewal and subprocess cleanup without live Feishu calls."""

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


def authorization(**user_fields):
    return {
        "appId": "cli_bot",
        "identities": {
            "user": {
                "status": "ready",
                "available": True,
                "tokenStatus": "valid",
                "openId": "ou_agent",
                "grantedAt": "2026-10-23T00:00:00Z",
                "expiresAt": "2026-10-23T02:00:00Z",
                "refreshExpiresAt": "2026-10-30T00:00:00Z",
                "scope": "offline_access im:chat.members:write_only",
                **user_fields,
            }
        },
    }


def success(data=None):
    return {"ok": True, "identity": "user", "data": data if data is not None else {}}


@pytest.fixture
def cli():
    adapter = LarkCLIAdapter(LarkCLIConfig(), "cli_bot")
    adapter._command = AsyncMock(
        side_effect=lambda *args: authorization() if args[0] == "auth" else success()
    )
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {"status": "missing", "available": False, "tokenStatus": ""},
        {"status": "not_configured", "available": False},
        {"tokenStatus": "expired"},
        {"openId": ""},
        {"status": "ready", "tokenStatus": "ready"},  # No built-in OAuth login.
    ],
)
async def test_unusable_login_never_executes_user_api_or_invitation(cli, fields):
    cli._command.return_value = authorization(**fields)
    cli._command.side_effect = None
    messages = []
    sink = logger.add(lambda message: messages.append(str(message)))
    try:
        for _ in range(3):
            await cli.keepalive_once()
            assert await cli.invite_bot("oc_ticket") is None
    finally:
        logger.remove(sink)
    assert len(cli._command.call_args_list) == 6
    assert all(call.args == ("auth", "status", "--json") for call in cli._command.call_args_list)
    assert len(messages) == 1


@pytest.mark.asyncio
async def test_wrong_application_skips_all_user_operations(cli):
    cli._command.side_effect = None
    cli._command.return_value = {**authorization(), "appId": "cli_other"}
    await cli.keepalive_once()
    assert await cli.invite_bot("oc_ticket") is None
    assert all(call.args[0] == "auth" for call in cli._command.call_args_list)


@pytest.mark.asyncio
async def test_unavailable_local_auth_skips_ticket_invites_without_repeated_errors(cli):
    cli._command.side_effect = LarkCLIError("command failed")
    messages = []
    sink = logger.add(lambda message: messages.append(str(message)))
    try:
        for _ in range(3):
            assert await cli.invite_bot("oc_ticket") is None
            await cli.keepalive_once()
    finally:
        logger.remove(sink)
    assert len(messages) == 1
    assert all(call.args[0] == "auth" for call in cli._command.call_args_list)


@pytest.mark.asyncio
async def test_transient_local_status_failure_can_be_retried_by_admission(cli):
    cli._command.side_effect = LarkCLIError("timed out", retryable=True)
    with pytest.raises(LarkCLIError) as failure:
        await cli.invite_bot("oc_ticket")
    assert failure.value.retryable
    assert cli._command.call_args.args == ("auth", "status", "--json")


@pytest.mark.asyncio
async def test_keepalive_calls_real_user_endpoint_only_when_due(cli):
    await cli.keepalive_once()
    await cli.keepalive_once()
    calls = [call.args for call in cli._command.call_args_list]
    assert calls == [
        ("auth", "status", "--json"),
        ("api", "GET", "/open-apis/authen/v1/user_info", "--as", "user", "--format", "json"),
        ("auth", "status", "--json"),
    ]


@pytest.mark.asyncio
async def test_refreshable_login_is_renewed_without_waiting_for_regular_interval(cli):
    cli._next_keepalive = time.monotonic() + 1800
    cli._command.side_effect = lambda *args: (
        authorization(status="needs_refresh", tokenStatus="needs_refresh")
        if args[0] == "auth"
        else success()
    )
    await cli.keepalive_once()
    assert cli._command.call_args.args[0] == "api"


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
@pytest.mark.parametrize("operation", ["keepalive", "invite"])
async def test_revoked_authorization_is_not_retried_until_credentials_change(cli, operation):
    user_status = authorization()

    def command(*args):
        if args[0] == "auth":
            return user_status
        raise LarkCLIError("command failed", code=20037, authentication=True)

    cli._command.side_effect = command
    if operation == "invite":
        assert await cli.invite_bot("oc_ticket") is None
    else:
        await cli.keepalive_once()
    await cli.keepalive_once()
    assert await cli.invite_bot("oc_ticket") is None
    assert sum(call.args[0] != "auth" for call in cli._command.call_args_list) == 1

    user_status = authorization(grantedAt="2026-10-23T01:00:00Z")
    cli._command.side_effect = lambda *args: user_status if args[0] == "auth" else success()
    await cli.keepalive_once()
    assert cli._command.call_args.args[0] == "api"


@pytest.mark.asyncio
async def test_transient_failures_back_off_and_recover(cli):
    def command(*args):
        if args[0] == "auth":
            return authorization(status="needs_refresh", tokenStatus="needs_refresh")
        raise LarkCLIError("command failed", code=503)

    cli._command.side_effect = command
    await cli.keepalive_once()
    first_retry = cli._retry_at
    await cli.keepalive_once()
    assert sum(call.args[0] == "api" for call in cli._command.call_args_list) == 1
    assert first_retry > time.monotonic()
    cli._retry_at = 0
    await cli.keepalive_once()
    assert cli._retry_at > time.monotonic() + 50
    cli._command.side_effect = lambda *args: authorization() if args[0] == "auth" else success()
    cli._retry_at = 0
    await cli.keepalive_once()
    assert cli._failures == 0
    assert cli._next_keepalive > time.monotonic()


@pytest.mark.asyncio
async def test_background_detects_new_login_without_restart_and_stops_cleanly():
    cli = LarkCLIAdapter(LarkCLIConfig(auth_check_interval_seconds=0.01), "cli_bot")
    inspected = asyncio.Event()
    kept_alive = asyncio.Event()
    logged_in = False

    def command(*args):
        if args[0] == "auth":
            inspected.set()
            return (
                authorization() if logged_in else authorization(available=False, status="missing")
            )
        kept_alive.set()
        return success()

    cli._command = AsyncMock(side_effect=command)
    cli.start()
    running_task = cli._keepalive_task
    cli.start()
    assert cli._keepalive_task is running_task
    try:
        await asyncio.wait_for(inspected.wait(), timeout=1)
        assert not kept_alive.is_set()
        logged_in = True
        await asyncio.wait_for(kept_alive.wait(), timeout=1)
    finally:
        await cli.close()
    assert running_task.done()
    assert cli._keepalive_task is None
    await cli.close()


@pytest.mark.asyncio
async def test_invite_and_keepalive_share_one_operation_lock(cli):
    entered, release = asyncio.Event(), asyncio.Event()

    async def command(*args):
        if args[0] == "auth":
            return authorization()
        if args[0] == "api":
            entered.set()
            await release.wait()
        return success()

    cli._command.side_effect = command
    keepalive = asyncio.create_task(cli.keepalive_once())
    await asyncio.wait_for(entered.wait(), timeout=1)
    invitation = asyncio.create_task(cli.invite_bot("oc_ticket"))
    await asyncio.sleep(0)
    assert not any(call.args[0] == "im" for call in cli._command.call_args_list)
    release.set()
    await asyncio.gather(keepalive, invitation)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [{"code": 0, "data": {}}, {"ok": True, "identity": "bot", "data": {}}, success([])],
)
async def test_invalid_success_envelope_cannot_be_treated_as_user_invitation(cli, response):
    cli._command.side_effect = lambda *args: authorization() if args[0] == "auth" else response
    with pytest.raises(LarkCLIError, match="invalid user"):
        await cli.invite_bot("oc_ticket")


@pytest.mark.asyncio
async def test_subprocess_uses_profile_without_shell_and_suppresses_notifiers(monkeypatch):
    process = SimpleNamespace(
        returncode=0, communicate=AsyncMock(return_value=(b'{"appId":"cli_bot"}', b""))
    )
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    cli = LarkCLIAdapter(
        LarkCLIConfig(executable="/path with spaces/lark-cli", profile="support"), "cli_bot"
    )
    assert await cli._command("auth", "status", "--json") == {"appId": "cli_bot"}
    assert spawn.call_args.args == (
        "/path with spaces/lark-cli",
        "--profile",
        "support",
        "auth",
        "status",
        "--json",
    )
    assert spawn.call_args.kwargs["stdin"] == asyncio.subprocess.DEVNULL
    assert spawn.call_args.kwargs["env"]["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] == "1"


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
        ({"type": "permission", "code": 232017}, False),
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
async def test_missing_cli_is_a_quiet_retryable_state(monkeypatch):
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError))
    cli = LarkCLIAdapter(LarkCLIConfig(), "cli_bot")
    await cli.keepalive_once()
    assert cli._notice == "cli_error"
    assert cli._retry_at > time.monotonic()


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
