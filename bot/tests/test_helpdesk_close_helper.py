"""One-shot closer: OAuth delegation, commit gating and native API outcomes."""

import asyncio
import base64
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from vikingbot.extensions.feishu_helpdesk import close_helper as helper
from vikingbot.extensions.feishu_helpdesk.cli_credentials import (
    PROVIDER_ENV,
    AccessToken,
    CredentialError,
    LocalSession,
)


@pytest.fixture
def request_data():
    return {
        "protocol_version": 1,
        "request_id": "close-round-1",
        "app_id": "cli_bot",
        "user_id": "ou_agent",
        "ticket_id": "123",
        "executable": "lark-cli",
        "profile": "support",
        "brand": "feishu",
        "authorization": base64.b64encode(b"desk:fake-service-token").decode(),
        "body": {"status": 51, "solved": 2, "comment": "业务确认恢复"},
        "timeout_seconds": 5,
    }


@pytest.fixture
def operation(request_data, monkeypatch):
    session = LocalSession("cli_bot", "ou_agent", "support", "local", "feishu")
    locked = []

    @asynccontextmanager
    async def lock(*args, **kwargs):
        locked.append(True)
        try:
            yield
        finally:
            locked.pop()

    def token(*args, **kwargs):
        assert locked
        return AccessToken("fake-user-token", time.time() + 3600)

    monkeypatch.setattr(helper, "refresh_lock", lock)
    monkeypatch.setattr(helper, "read_access_token", token)
    monkeypatch.setattr(helper, "verify_profile", lambda *a, **kw: None)
    probe = SimpleNamespace(prepare=AsyncMock(return_value=session), renew=AsyncMock())
    records, calls = [], []
    response = {"status": 200, "body": {"code": 0, "data": {}}}

    async def receive():
        assert locked
        return {"protocol_version": 1, "request_id": request_data["request_id"], "action": "commit"}

    async def handler(request):
        calls.append(request)
        if "error" in response:
            raise response["error"]
        return httpx.Response(response["status"], json=response["body"])

    def client_factory(**kwargs):
        assert kwargs["follow_redirects"] is False
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    op = helper.CloseOperation(
        helper.CloseRequest.parse(request_data),
        receive,
        records.append,
        probe=probe,
        client_factory=client_factory,
    )
    return SimpleNamespace(
        op=op,
        probe=probe,
        records=records,
        calls=calls,
        response=response,
        locked=locked,
        session=session,
        request_data=request_data,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("solved", [1, 2])
async def test_put_has_both_authorizations_and_only_follows_commit(operation, solved):
    case = operation
    case.op.request.body["solved"] = solved
    await case.op.run()
    assert [r["phase"] for r in case.records] == ["ready", "result"]
    assert case.records[-1]["outcome"] == "accepted"
    assert len(case.calls) == 1
    request = case.calls[0]
    assert request.method == "PUT"
    assert str(request.url) == "https://open.feishu.cn/open-apis/helpdesk/v1/tickets/123"
    assert request.headers["Authorization"] == "Bearer fake-user-token"
    assert request.headers["X-Lark-Helpdesk-Authorization"] == case.request_data["authorization"]
    assert json.loads(request.content) == {
        "status": 51,
        "solved": solved,
        "comment": "业务确认恢复",
    }
    assert "fake-user-token" not in json.dumps(case.records)
    assert case.request_data["authorization"] not in json.dumps(case.records)
    assert not case.locked


@pytest.mark.asyncio
async def test_parent_abort_never_calls_close_api(operation):
    case = operation
    case.op.receive = AsyncMock(
        return_value={"protocol_version": 1, "request_id": "close-round-1", "action": "abort"}
    )
    await case.op.run()
    assert not case.calls and not case.op.submitted
    assert case.records[-1]["outcome"] == "cancelled"
    assert not case.locked


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "control",
    [
        {},
        {"protocol_version": 1, "request_id": "another-round", "action": "commit"},
        {"protocol_version": 2, "request_id": "close-round-1", "action": "commit"},
        {"protocol_version": True, "request_id": "close-round-1", "action": "commit"},
    ],
)
async def test_commit_must_match_request_and_protocol(operation, control):
    operation.op.receive = AsyncMock(return_value=control)
    await operation.op.run()
    assert not operation.calls
    assert operation.records[-1]["outcome"] == "rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,outcome,authentication",
    [
        (403, {"code": 154003}, "rejected", False),
        (401, {"code": 154001}, "rejected", True),
        (400, {"code": 154000}, "rejected", False),
        (404, {"code": 154004}, "rejected", False),
        (429, {"code": 99991400}, "rejected", False),
        (401, {"code": 99991661}, "rejected", True),
        (401, {"code": 99991663}, "rejected", True),
        (400, {"code": 99991668}, "rejected", True),
        (401, {"code": 99991671}, "rejected", True),
        (400, {"code": 99991677}, "rejected", True),
        (403, {"code": 230027}, "rejected", False),
        (400, {"code": 99991662}, "rejected", False),
        (403, {"code": 99991672}, "rejected", False),
        (400, {"code": 99991673}, "rejected", False),
        (403, {"code": 99991676}, "rejected", False),
        (403, {"code": 99991679}, "rejected", False),
        (200, {"code": 99991672}, "rejected", False),
        (200, {"code": 99991677}, "rejected", True),
        (500, {"code": 155000}, "uncertain", False),
        (500, {"code": 99991668}, "uncertain", False),
        (503, {"code": 99991672}, "uncertain", False),
        (403, {"code": 154099}, "uncertain", False),
        (200, {"ok": True, "data": {}}, "uncertain", False),  # CLI != native API contract.
        (200, {"code": False, "data": {}}, "uncertain", False),
        (200, {"code": "0", "data": {}}, "uncertain", False),
        (200, ["unexpected"], "uncertain", False),
        (200, {"code": 0}, "accepted", False),
        (302, {"code": 0, "data": {}}, "uncertain", False),
    ],
)
async def test_api_failure_is_classified_without_retry(
    operation, status, body, outcome, authentication
):
    operation.response.update(status=status, body=body)
    await operation.op.run()
    assert len(operation.calls) == 1
    record = operation.records[-1]
    assert record["outcome"] == outcome
    if outcome == "accepted":
        assert "error" not in record
    else:
        assert record["error"]["authentication"] is authentication


@pytest.mark.asyncio
async def test_lost_response_remains_uncertain_and_hides_exception_details(operation):
    operation.response["error"] = httpx.ReadTimeout("fake-user-token fake-service-token")
    await operation.op.run()
    assert len(operation.calls) == 1
    assert operation.records[-1]["outcome"] == "uncertain"
    assert "fake-user-token" not in json.dumps(operation.records)
    assert "fake-service-token" not in json.dumps(operation.records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [OSError("cleanup failed"), CredentialError("CLI refresh lock is unavailable")]
)
async def test_post_submission_cleanup_failure_is_uncertain_and_has_one_result(
    operation, monkeypatch, error
):
    @asynccontextmanager
    async def lock(*args, **kwargs):
        operation.locked.append(True)
        try:
            yield
            raise error
        finally:
            operation.locked.pop()

    monkeypatch.setattr(helper, "refresh_lock", lock)
    await operation.op.run()
    assert len(operation.calls) == 1
    assert [r["phase"] for r in operation.records] == ["ready", "result"]
    assert operation.records[-1]["outcome"] == "uncertain"


@pytest.mark.asyncio
async def test_lark_brand_uses_only_lark_official_origin(operation):
    operation.probe.prepare.return_value = LocalSession(
        "cli_bot", "ou_agent", "support", "local", "lark"
    )
    await operation.op.run()
    assert (
        str(operation.calls[0].url)
        == "https://open.larksuite.com/open-apis/helpdesk/v1/tickets/123"
    )


@pytest.mark.asyncio
async def test_timeout_waiting_for_parent_is_pre_submission(operation):
    operation.op.deadline = time.monotonic() + 0.02

    async def wait():
        await asyncio.Event().wait()

    operation.op.receive = wait
    await operation.op.run()
    assert not operation.calls
    assert operation.records[-1]["outcome"] == "rejected"
    assert not operation.locked


@pytest.mark.asyncio
async def test_token_renewal_releases_refresh_lock_before_invoking_cli(operation, monkeypatch):
    reads = 0

    def read(*args, **kwargs):
        nonlocal reads
        assert operation.locked
        reads += 1
        if reads == 1:
            raise CredentialError("CLI user token needs renewal")
        return AccessToken("fresh-user-token", time.time() + 3600)

    async def renew(session):
        assert not operation.locked

    monkeypatch.setattr(helper, "read_access_token", read)
    operation.probe.renew.side_effect = renew
    await operation.op.run()
    operation.probe.renew.assert_awaited_once_with(operation.session)
    assert operation.calls[0].headers["Authorization"] == "Bearer fresh-user-token"


@pytest.mark.asyncio
async def test_profile_change_after_ready_aborts_without_put(operation, monkeypatch):
    monkeypatch.setattr(
        helper,
        "verify_profile",
        lambda *a, **kw: (_ for _ in ()).throw(
            CredentialError("CLI profile changed or has an unsupported format")
        ),
    )
    await operation.op.run()
    assert not operation.calls
    assert operation.records[-1]["outcome"] == "rejected"


@pytest.mark.asyncio
async def test_official_cli_dry_run_and_user_read_are_checked_before_ready(
    request_data, monkeypatch
):
    probe = helper.CLIProbe(helper.CloseRequest.parse(request_data))
    calls = []
    config = {"profile": "support", "workspace": "local", "appId": "cli_bot", "brand": "feishu"}
    status = {
        "appId": "cli_bot",
        "brand": "feishu",
        "identities": {
            "user": {
                "openId": "ou_agent",
                "available": True,
                "status": "ready",
                "tokenStatus": "valid",
            }
        },
    }

    async def run(*args):
        calls.append(args)
        if args[:2] == ("config", "show"):
            return config
        if args[:2] == ("auth", "status"):
            return status
        if "--dry-run" in args:
            assert "--headers" not in args
            assert request_data["authorization"] not in args
            return {"as": "user", "appId": "cli_bot", "userOpenId": "ou_agent"}
        return {"ok": True, "identity": "user", "data": {"open_id": "ou_agent"}}

    monkeypatch.setattr(helper, "verify_profile", lambda *a, **kw: None)
    for key in PROVIDER_ENV:
        monkeypatch.delenv(key, raising=False)
    probe.run = AsyncMock(side_effect=run)
    session = await probe.prepare()
    assert session.user_id == "ou_agent"
    dry = next(i for i, args in enumerate(calls) if "--dry-run" in args)
    read = next(i for i, args in enumerate(calls) if "/open-apis/authen/v1/user_info" in args)
    assert dry < read
    assert all("fake-service-token" not in json.dumps(args) for args in calls)


@pytest.mark.asyncio
async def test_official_cli_current_dry_run_context_identity_is_accepted(
    request_data, monkeypatch
):
    probe = helper.CLIProbe(helper.CloseRequest.parse(request_data))
    config = {"profile": "support", "workspace": "local", "appId": "cli_bot", "brand": "feishu"}
    status = {
        "appId": "cli_bot",
        "brand": "feishu",
        "identities": {
            "user": {
                "openId": "ou_agent",
                "available": True,
                "status": "ready",
                "tokenStatus": "valid",
            }
        },
    }

    async def run(*args):
        if args[:2] == ("config", "show"):
            return config
        if args[:2] == ("auth", "status"):
            return status
        if "--dry-run" in args:
            return {
                "ok": True,
                "identity": "user",
                "dry_run": True,
                "data": {
                    "api": [{"method": "PUT", "url": request_data["ticket_id"]}],
                    "context": {"app_id": "cli_bot", "user_open_id": "ou_agent"},
                },
            }
        return {"ok": True, "identity": "user", "data": {"open_id": "ou_agent"}}

    monkeypatch.setattr(helper, "verify_profile", lambda *a, **kw: None)
    for key in PROVIDER_ENV:
        monkeypatch.delenv(key, raising=False)
    probe.run = AsyncMock(side_effect=run)
    session = await probe.prepare()
    assert session.user_id == "ou_agent"


@pytest.mark.asyncio
async def test_cli_user_read_cannot_be_mistaken_for_native_code_zero(request_data):
    probe = helper.CLIProbe(helper.CloseRequest.parse(request_data))
    probe.run = AsyncMock(return_value={"code": 0, "data": {"open_id": "ou_agent"}})
    session = LocalSession("cli_bot", "ou_agent", "support", "local", "feishu")
    with pytest.raises(helper.CloseFailure, match="expected user"):
        await probe.renew(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"brand": "lark"}, {"profile": "another-profile"}])
async def test_preparation_refuses_changed_profile_or_channel_brand(
    request_data, monkeypatch, change
):
    probe = helper.CLIProbe(helper.CloseRequest.parse(request_data))
    config = {
        "profile": "support",
        "workspace": "local",
        "appId": "cli_bot",
        "brand": "feishu",
        **change,
    }
    status = {
        "appId": "cli_bot",
        "brand": config["brand"],
        "identities": {
            "user": {
                "openId": "ou_agent",
                "available": True,
                "status": "ready",
                "tokenStatus": "valid",
            }
        },
    }
    for key in PROVIDER_ENV:
        monkeypatch.delenv(key, raising=False)
    probe.run = AsyncMock(side_effect=[config, status])
    with pytest.raises(helper.CloseFailure, match="profile or brand"):
        await probe.prepare()
    # No dry-run or renewal proceeds under a different configured identity.
    assert probe.run.await_count == 2


@pytest.mark.asyncio
async def test_cli_policy_refusal_never_reaches_renewal_or_token_read(request_data, monkeypatch):
    probe = helper.CLIProbe(helper.CloseRequest.parse(request_data))
    config = {"profile": "support", "workspace": "local", "appId": "cli_bot", "brand": "feishu"}
    status = {
        "appId": "cli_bot",
        "brand": "feishu",
        "identities": {
            "user": {
                "openId": "ou_agent",
                "available": True,
                "status": "ready",
                "tokenStatus": "valid",
            }
        },
    }
    for key in PROVIDER_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(helper, "verify_profile", lambda *a, **kw: None)
    probe.run = AsyncMock(
        side_effect=[config, status, helper.CloseFailure("CLI command policy refused")]
    )
    probe.renew = AsyncMock()
    with pytest.raises(helper.CloseFailure, match="policy refused"):
        await probe.prepare()
    assert "--dry-run" in probe.run.await_args.args
    probe.renew.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    [
        {"body": {"status": 50, "solved": 2}},
        {"body": {"status": 51, "solved": True}},
        {"body": {"status": 51, "solved": 2, "channel": 7}},
        {"authorization": "fake\r\nheader"},
        {"timeout_seconds": False},
        {"timeout_seconds": 0},
        {"ticket_id": ""},
        {"protocol_version": 2},
        {"protocol_version": True},
        {"brand": "https://example.com"},
        {"ticket_id": ".."},
        {"ticket_id": "123/../456"},
    ],
)
def test_helper_only_accepts_bounded_close_operations(request_data, change):
    with pytest.raises(helper.CloseFailure, match="invalid close request"):
        helper.CloseRequest.parse({**request_data, **change})
