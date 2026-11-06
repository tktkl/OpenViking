import json
from pathlib import Path

import httpx
import pytest
from vikingbot.agent.tools.base import ToolContext
from vikingbot.agent.tools.send_file import SendFileTool
from vikingbot.channels.feishu_files import FeishuFileSendError, send_files
from vikingbot.config.schema import SessionKey


class FakeSandbox:
    def __init__(self, workspace: Path):
        self.workspace = workspace

    def local_file_path(self, path: str) -> Path:
        target = self.workspace / path
        if not target.exists():
            raise FileNotFoundError(path)
        return target

    async def read_file_bytes(self, path: str) -> bytes:
        return (self.workspace / path).read_bytes()


class FakeSandboxManager:
    def __init__(self, workspace: Path):
        self.sandbox = FakeSandbox(workspace)

    async def get_sandbox(self, session_key):
        return self.sandbox


@pytest.mark.asyncio
async def test_send_file_queues_workspace_file_with_metadata(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "work").mkdir(parents=True)
    payload = b"PK\x03\x04xlsx-test"
    (workspace / "work" / "report.xlsx").write_bytes(payload)
    sent = []

    async def send(message):
        sent.append(message)

    tool = SendFileTool(send_callback=send)
    context = ToolContext(
        session_key=SessionKey(type="feishu", channel_id="cli_bot", chat_id="oc_chat"),
        sandbox_manager=FakeSandboxManager(workspace),
        channel_metadata={"reply_to": "oc_chat", "message_id": "om_trigger"},
    )

    result = await tool.execute(context, path="work/report.xlsx")

    assert result == "File send queued: report.xlsx (13 bytes)"
    assert len(sent) == 1
    outbound = sent[0]
    assert outbound.content == ""
    assert outbound.metadata["reply_to"] == "oc_chat"
    assert outbound.files == [{"filename": "report.xlsx", "content": payload}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/etc/passwd", "../secret.txt", "work/../../secret.txt", "C:\\temp\\a.xlsx"]
)
async def test_send_file_rejects_paths_outside_workspace(tmp_path, path):
    tool = SendFileTool(send_callback=pytest.fail)
    context = ToolContext(
        session_key=SessionKey(type="feishu", channel_id="cli_bot", chat_id="oc_chat"),
        sandbox_manager=FakeSandboxManager(tmp_path),
    )

    result = await tool.execute(context, path=path)

    assert result.startswith("Error: path must be a relative path")


@pytest.mark.asyncio
async def test_send_file_rejects_non_feishu_channel(tmp_path):
    tool = SendFileTool(send_callback=pytest.fail)
    context = ToolContext(
        session_key=SessionKey(type="telegram", channel_id="bot", chat_id="123"),
        sandbox_manager=FakeSandboxManager(tmp_path),
    )

    result = await tool.execute(context, path="report.xlsx")

    assert result == "Error: File sending is currently supported only for Feishu chats"


@pytest.mark.asyncio
async def test_feishu_file_send_uploads_and_replies(monkeypatch):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/open-apis/im/v1/files":
            assert b'name="file_name"' in request.content
            assert b"report.xlsx" in request.content
            return httpx.Response(200, json={"code": 0, "data": {"file_key": "file_123"}})
        assert request.url.path == "/open-apis/im/v1/messages/om_parent/reply"
        body = json.loads(request.content)
        assert body["msg_type"] == "file"
        assert json.loads(body["content"])["file_key"] == "file_123"
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_file"}})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "vikingbot.channels.feishu_files.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )

    await send_files(
        domain="https://open.feishu.cn",
        access_token="tenant-token",
        receive_id="oc_chat",
        files=[
            {
                "filename": "report.xlsx",
                "content": b"xlsx-data",
            }
        ],
        reply_to_message_id="om_parent",
    )

    assert len(requests) == 2
    assert requests[0].headers["authorization"] == "Bearer tenant-token"


@pytest.mark.asyncio
async def test_feishu_file_send_raises_safe_upload_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": 234001, "msg": "private response"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "vikingbot.channels.feishu_files.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )

    with pytest.raises(FeishuFileSendError) as raised:
        await send_files(
            domain="https://open.feishu.cn",
            access_token="tenant-token",
            receive_id="oc_chat",
            files=[{"filename": "report.xlsx", "content": b"xlsx-data"}],
        )

    assert raised.value.stage == "upload"
    assert raised.value.status == 400
    assert raised.value.code == 234001
    assert "private response" not in str(raised.value)
