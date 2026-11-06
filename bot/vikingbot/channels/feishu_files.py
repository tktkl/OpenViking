"""Minimal Feishu file-message delivery for bot identity."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import httpx


class FeishuFileSendError(RuntimeError):
    """A file delivery failure with a safe diagnostic boundary."""

    def __init__(self, filename: str, stage: str, *, status: int = 0, code: object = None):
        super().__init__(f"Feishu file {stage} failed for {filename} (HTTP={status}, code={code})")
        self.filename = filename
        self.stage = stage
        self.status = status
        self.code = code


def _result_code(payload: Any) -> object:
    return payload.get("code") if isinstance(payload, dict) else None


async def send_files(
    *,
    domain: str,
    access_token: str,
    receive_id: str,
    files: list[dict[str, Any]],
    reply_to_message_id: str | None = None,
    reply_in_thread: bool = False,
    timeout: float = 120.0,
) -> None:
    """Upload and send files as native Feishu file messages.

    The caller passes bytes in-process.  No filesystem path is exposed to the
    transport, and no application-level byte-size cap is applied here.
    """
    if not files:
        return
    receive_id_type = "chat_id" if receive_id.startswith("oc_") else "open_id"
    headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient(timeout=timeout) as client:
        for item in files:
            filename = str(item.get("filename") or "file.bin")
            content = item.get("content")
            if not isinstance(content, (bytes, bytearray, memoryview)) or not content:
                raise FeishuFileSendError(filename, "payload")
            upload = await client.post(
                f"{domain.rstrip('/')}/open-apis/im/v1/files",
                headers=headers,
                data={"file_type": "stream", "file_name": filename},
                files={"file": (filename, bytes(content))},
            )
            try:
                upload_payload = upload.json()
            except ValueError:
                upload_payload = None
            if upload.is_error or _result_code(upload_payload) != 0:
                raise FeishuFileSendError(
                    filename,
                    "upload",
                    status=upload.status_code,
                    code=_result_code(upload_payload),
                )
            file_key = ((upload_payload or {}).get("data") or {}).get("file_key")
            if not isinstance(file_key, str) or not file_key:
                raise FeishuFileSendError(filename, "upload", status=upload.status_code)

            body = {
                "msg_type": "file",
                "content": json.dumps({"file_key": file_key}, ensure_ascii=False),
            }
            if reply_to_message_id:
                body["reply_in_thread"] = bool(reply_in_thread)
                response = await client.post(
                    f"{domain.rstrip('/')}/open-apis/im/v1/messages/"
                    f"{quote(reply_to_message_id, safe='')}/reply",
                    headers=headers,
                    json=body,
                )
            else:
                response = await client.post(
                    f"{domain.rstrip('/')}/open-apis/im/v1/messages",
                    headers=headers,
                    params={"receive_id_type": receive_id_type},
                    json={
                        **body,
                        "receive_id": receive_id,
                    },
                )
            try:
                response_payload = response.json()
            except ValueError:
                response_payload = None
            if response.is_error or _result_code(response_payload) != 0:
                raise FeishuFileSendError(
                    filename,
                    "send",
                    status=response.status_code,
                    code=_result_code(response_payload),
                )
