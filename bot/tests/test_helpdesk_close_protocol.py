"""Close-ticket guard ordering through the auth-mcp-backed lark-cli adapter."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from vikingbot.extensions.feishu_helpdesk.cli import LarkCLIAdapter, LarkCLIError
from vikingbot.extensions.feishu_helpdesk.config import LarkCLIConfig


def success():
    return {"ok": True, "identity": "user", "data": {}}


def auth_value(*, token="user-token", state="ready"):
    return {
        "state": state,
        "identity": "ou_agent",
        "access": {"headers": {"Authorization": f"Bearer {token}"}},
    }


@pytest.fixture
def adapter():
    adapter = LarkCLIAdapter(LarkCLIConfig(timeout_seconds=3), "cli_bot")
    adapter._auth_access = AsyncMock(return_value=auth_value())
    adapter._command = AsyncMock(return_value=success())
    return adapter


@pytest.mark.asyncio
async def test_close_uses_one_put_after_business_guard(adapter):
    order = []

    async def before(actor):
        order.append(("before", actor))
        assert adapter._command.await_count == 0

    adapter._command.side_effect = lambda *args, **kwargs: (
        order.append(("command", args[1])) or success()
    )
    result = await adapter.close_ticket(
        "123", "service-secret", {"status": 51, "solved": 2}, before_call=before
    )
    assert result == {}
    assert order == [("before", "ou_agent"), ("command", "PUT")]
    assert adapter._command.await_count == 1


@pytest.mark.asyncio
async def test_stale_business_guard_never_calls_cli(adapter):
    async def before(actor):
        raise ValueError("new human message")

    with pytest.raises(ValueError, match="new human message"):
        await adapter.close_ticket(
            "123", "service-secret", {"status": 51, "solved": 1}, before_call=before
        )
    assert adapter._command.await_count == 0


@pytest.mark.asyncio
async def test_authentication_failure_refreshes_token_before_retry(adapter):
    calls = 0

    def command(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise LarkCLIError("command failed", code=20037, authentication=True)
        return success()

    adapter._command.side_effect = command
    before = AsyncMock(return_value=None)
    assert await adapter.close_ticket(
        "123", "service-secret", {"status": 51, "solved": 1}, before_call=before
    ) == {}
    assert calls == 2
    assert adapter._auth_access.await_count == 2
    assert before.await_count == 2


@pytest.mark.asyncio
async def test_auth_not_ready_rejects_without_cli_call(adapter):
    adapter._auth_access.return_value = auth_value(state="waiting_scan")
    with pytest.raises(LarkCLIError, match="auth MCP"):
        await adapter.close_ticket("123", "service-secret", {"status": 51, "solved": 1})
    assert adapter._command.await_count == 0


@pytest.mark.asyncio
async def test_cancellation_while_waiting_for_lock_does_not_call_cli(adapter):
    async with adapter._lock:
        task = asyncio.create_task(
            adapter.close_ticket("123", "service-secret", {"status": 51, "solved": 1})
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert adapter._command.await_count == 0
