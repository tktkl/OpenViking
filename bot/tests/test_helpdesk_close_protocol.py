"""Real subprocess pipes and process groups; all CLI/network peers are fakes."""

import asyncio
import base64
import json
import os
import sys
from unittest.mock import AsyncMock

import pytest
from vikingbot.extensions.feishu_helpdesk.cli import LarkCLIAdapter, LarkCLIError
from vikingbot.extensions.feishu_helpdesk.config import LarkCLIConfig

FAKE_HELPER = r"""
import json, os, subprocess, sys, time
request = json.loads(sys.stdin.readline())
mode = os.environ['FAKE_CLOSE_MODE']
events = os.environ['FAKE_CLOSE_EVENTS']
def log(value):
    with open(events, 'a') as f:
        f.write(json.dumps(value) + '\n')
def emit(phase, **fields):
    print(json.dumps(dict(protocol_version=1, request_id=request['request_id'], phase=phase, **fields)), flush=True)
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
log({'child': child.pid})
if mode == 'before':
    time.sleep(60)
elif mode == 'invalid':
    print('fake-user-secret fake-service-secret', file=sys.stderr, flush=True)
    print('unsupported protocol', flush=True)
    sys.exit(1)
elif mode in ('credentials', 'secret_diagnostic'):
    reason = 'unsupported or invalid CLI credential format' if mode == 'credentials' else 'fake-user-secret'
    emit('result', outcome='rejected', error={'kind': 'credentials', 'reason': reason})
    sys.exit(0)
emit('ready', app_id=request['app_id'], user_id=request['user_id'])
control = json.loads(sys.stdin.readline())
log({'action': control['action']})
if mode == 'after':
    time.sleep(60)
if mode == 'rejected':
    emit('result', outcome='rejected', error={'code':154003})
else:
    emit('result', outcome='accepted')
"""


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    log = tmp_path / "helper-events"
    monkeypatch.setenv("FAKE_CLOSE_EVENTS", str(log))
    monkeypatch.setenv("FAKE_CLOSE_MODE", "accepted")
    adapter = LarkCLIAdapter(LarkCLIConfig(timeout_seconds=3), "cli_bot")
    adapter._authorization = AsyncMock(return_value={"openId": "ou_agent"})
    original = asyncio.create_subprocess_exec
    spawned = []

    async def start(*args, **kwargs):
        spawned.append(args)
        assert args == (sys.executable, "-m", "vikingbot.extensions.feishu_helpdesk.close_helper")
        return await original(sys.executable, "-c", FAKE_HELPER, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    adapter.events, adapter.spawned = log, spawned
    return adapter


def events(adapter):
    if not adapter.events.exists():
        return []
    return [json.loads(line) for line in adapter.events.read_text().splitlines()]


async def assert_child_gone(adapter):
    pids = [event["child"] for event in events(adapter) if "child" in event]
    assert pids
    for pid in pids:
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("helper's CLI descendant was not reaped")


@pytest.mark.asyncio
async def test_real_pipes_only_commit_after_business_guard(adapter):
    async def before(actor):
        assert actor == "ou_agent"
        assert not any("action" in event for event in events(adapter))

    assert (
        await adapter.close_ticket(
            "123", "fake-service-secret", {"status": 51, "solved": 2}, before_call=before
        )
        == {}
    )
    assert {"action": "commit"} in events(adapter)
    assert len(adapter.spawned) == 1
    await assert_child_gone(adapter)


@pytest.mark.asyncio
async def test_stale_business_guard_never_commits_and_reaps_all_children(adapter):
    async def before(actor):
        raise ValueError("new human message")

    with pytest.raises(ValueError, match="new human message"):
        await adapter.close_ticket(
            "123", "fake-service-secret", {"status": 51, "solved": 1}, before_call=before
        )
    assert not any(event.get("action") == "commit" for event in events(adapter))
    await assert_child_gone(adapter)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,rejected", [("before", True), ("after", False)])
async def test_timeout_preserves_submission_boundary(adapter, monkeypatch, phase, rejected):
    monkeypatch.setenv("FAKE_CLOSE_MODE", phase)
    adapter.settings.timeout_seconds = 0.5
    with pytest.raises(LarkCLIError) as error:
        await adapter.close_ticket("123", "fake-service-secret", {"status": 51, "solved": 1})
    assert error.value.rejected is rejected
    await assert_child_gone(adapter)


@pytest.mark.asyncio
async def test_timeout_waiting_for_shared_lock_does_not_start_helper(adapter):
    adapter.settings.timeout_seconds = 0.02
    async with adapter._lock:
        with pytest.raises(LarkCLIError) as error:
            await adapter.close_ticket("123", "fake-service-secret", {"status": 51, "solved": 1})
    assert error.value.rejected
    assert not adapter.spawned


@pytest.mark.asyncio
async def test_cancellation_after_commit_retains_uncertain_submission(adapter, monkeypatch):
    monkeypatch.setenv("FAKE_CLOSE_MODE", "after")
    task = asyncio.create_task(
        adapter.close_ticket("123", "fake-service-secret", {"status": 51, "solved": 2})
    )
    for _ in range(100):
        if {"action": "commit"} in events(adapter):
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("helper did not reach commit")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert {"action": "commit"} in events(adapter)
    await assert_child_gone(adapter)


@pytest.mark.asyncio
async def test_explicit_api_refusal_is_rejected_after_commit(adapter, monkeypatch):
    monkeypatch.setenv("FAKE_CLOSE_MODE", "rejected")
    with pytest.raises(LarkCLIError) as error:
        await adapter.close_ticket("123", "fake-service-secret", {"status": 51, "solved": 2})
    assert error.value.rejected and error.value.code == 154003
    await assert_child_gone(adapter)


@pytest.mark.asyncio
async def test_invalid_protocol_never_exposes_captured_stderr(adapter, monkeypatch):
    monkeypatch.setenv("FAKE_CLOSE_MODE", "invalid")
    with pytest.raises(LarkCLIError) as error:
        await adapter.close_ticket("123", "fake-service-secret", {"status": 51, "solved": 2})
    assert error.value.rejected
    assert "fake-service-secret" not in str(error.value)
    assert "fake-user-secret" not in str(error.value)
    await assert_child_gone(adapter)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["credentials", "secret_diagnostic"])
async def test_credential_errors_are_actionable_but_only_fixed_diagnostics_are_displayed(
    adapter, monkeypatch, mode
):
    monkeypatch.setenv("FAKE_CLOSE_MODE", mode)
    with pytest.raises(LarkCLIError) as error:
        await adapter.close_ticket("123", "fake-service-secret", {"status": 51, "solved": 2})
    assert error.value.rejected
    assert "compatible local CLI credentials" in str(error.value)
    if mode == "credentials":
        assert "unsupported or invalid CLI credential format" in str(error.value)
    assert "fake-user-secret" not in str(error.value)
    await assert_child_gone(adapter)


@pytest.mark.asyncio
async def test_custom_channel_domain_cannot_be_used_for_python_close():
    adapter = LarkCLIAdapter(LarkCLIConfig(), "cli_bot", domain="https://example.com")
    adapter._authorization = AsyncMock()
    with pytest.raises(LarkCLIError, match="official Feishu/Lark domain") as error:
        await adapter.close_ticket("123", "fake-service-secret", {"status": 51, "solved": 1})
    assert error.value.rejected
    adapter._authorization.assert_not_called()


@pytest.mark.asyncio
async def test_packaged_python_helper_prepares_real_encrypted_store_then_aborts(
    tmp_path, monkeypatch
):
    """No monkeypatch in the child: actual module, crypto and fake CLI processes."""
    import time

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from vikingbot.extensions.feishu_helpdesk.cli_credentials import PROVIDER_ENV

    for key in PROVIDER_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LARKSUITE_CLI_CONFIG_DIR", raising=False)
    monkeypatch.delenv("LARKSUITE_CLI_DATA_DIR", raising=False)
    for key in tuple(os.environ):
        if key.startswith(("HERMES_", "OPENCLAW_")) or key == "LARK_CHANNEL":
            monkeypatch.delenv(key, raising=False)
    config = {
        "apps": [
            {
                "name": "support",
                "appId": "cli_bot",
                "brand": "feishu",
                "users": [{"userOpenId": "ou_agent"}],
            }
        ]
    }
    runtime = tmp_path / ".lark-cli"
    runtime.mkdir()
    (runtime / "config.json").write_text(json.dumps(config))
    if sys.platform == "darwin":
        storage = tmp_path / "Library" / "Application Support" / "lark-cli"
        key_name = "master.key.file"
    else:
        storage, key_name = tmp_path / ".local" / "share" / "lark-cli", "master.key"
    storage.mkdir(parents=True)
    key, nonce = AESGCM.generate_key(bit_length=256), os.urandom(12)
    (storage / key_name).write_bytes(key)
    stored = {
        "appId": "cli_bot",
        "userOpenId": "ou_agent",
        "accessToken": "fake-access-secret",
        "refreshToken": "fake-refresh-secret",
        "scope": "offline_access helpdesk:all",
        "expiresAt": int((time.time() + 3600) * 1000),
    }
    (storage / "cli_bot_ou_agent.enc").write_bytes(
        nonce + AESGCM(key).encrypt(nonce, json.dumps(stored).encode(), None)
    )
    cli = tmp_path / "fake-cli"
    cli.write_text(
        f"#!{sys.executable}\n"
        + r"""
import json, sys
args = sys.argv[1:]
if args[:1] == ['--profile']: args = args[2:]
if args[:2] == ['config', 'show']:
    value = dict(profile='support', workspace='local', brand='feishu', appId='cli_bot')
elif args[:2] == ['auth', 'status']:
    value = dict(appId='cli_bot', brand='feishu', identities={'user': dict(openId='ou_agent', available=True, status='ready', tokenStatus='valid')})
elif '--dry-run' in args:
    print('=== Dry Run ===')
    value = dict(**{'as': 'user'}, appId='cli_bot', userOpenId='ou_agent')
else:
    assert args[:3] == ['api', 'GET', '/open-apis/authen/v1/user_info']
    value = dict(ok=True, identity='user', data=dict(open_id='ou_agent'))
print(json.dumps(value))
"""
    )
    cli.chmod(0o700)
    adapter = LarkCLIAdapter(
        LarkCLIConfig(executable=str(cli), profile="support", timeout_seconds=5), "cli_bot"
    )
    guard = AsyncMock(side_effect=ValueError("new human message"))
    with pytest.raises(ValueError, match="new human message"):
        await adapter.close_ticket(
            "123",
            base64.b64encode(b"desk:fake-service-token").decode(),
            {"status": 51, "solved": 2, "comment": "solved"},
            before_call=guard,
        )
    guard.assert_awaited_once_with("ou_agent")
    assert json.loads((runtime / "config.json").read_text()) == config
