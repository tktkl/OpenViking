"""Official lark-cli user operations using auth-mcp-issued credentials."""

import asyncio
import json
import os
import signal
import time
from urllib.parse import quote

import httpx
from loguru import logger

from vikingbot.extensions.feishu_helpdesk.config import LarkCLIConfig

_PROVIDER_ENV = (
    "LARKSUITE_CLI_APP_ID",
    "LARKSUITE_CLI_APP_SECRET",
    "LARKSUITE_CLI_USER_ACCESS_TOKEN",
    "LARKSUITE_CLI_TENANT_ACCESS_TOKEN",
    "LARKSUITE_CLI_AUTH_PROXY",
)


class LarkCLIError(RuntimeError):
    """Never include CLI stdout, stderr, request data or credentials in diagnostics."""

    def __init__(
        self, reason: str, *, code=None, authentication=False, retryable=False, rejected=False
    ):
        self.code = code if isinstance(code, int) else None
        self.authentication = authentication
        self.retryable = retryable and not authentication
        self.rejected = rejected
        super().__init__(f"Feishu Helpdesk lark-cli {reason} (code={self.code})")


def _json_output(raw: bytes) -> dict:
    content = raw.decode("utf-8", errors="replace")
    offset = 0
    for line in content.splitlines(keepends=True):
        if line.lstrip().startswith("{"):
            try:
                result, _ = json.JSONDecoder().raw_decode(content[offset:].lstrip())
                if isinstance(result, dict):
                    return result
            except ValueError:
                pass
        offset += len(line)
    return {}


def _value(source, name, default=None):
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


class LarkCLIAdapter:
    def __init__(
        self,
        settings: LarkCLIConfig,
        app_id: str,
        *,
        domain="https://open.feishu.cn",
        bot_config=None,
    ):
        self.settings = settings
        self.app_id = app_id
        self.bot_config = bot_config
        self.brand = {
            "https://open.feishu.cn": "feishu",
            "https://open.larksuite.com": "lark",
        }.get(domain.rstrip("/"))
        self._lock = asyncio.Lock()
        self._http_client = None
        self._auth_cache = None
        self._auth_cached_at = 0.0
        self._notice = ""

    def _report(self, state: str, message: str, *, warning=False):
        if state != self._notice:
            logger.log("WARNING" if warning else "INFO", "Feishu Helpdesk CLI: {}", message)
            self._notice = state

    def _client(self):
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=30, trust_env=False)
        return self._http_client

    def _auth_server(self):
        servers = _value(_value(self.bot_config, "tools", None), "mcp_servers", {}) or {}
        server = servers.get(self.settings.auth_mcp_server)
        if server is None:
            raise LarkCLIError("auth MCP server is not configured", rejected=True)
        return server

    async def _auth_access(self):
        server = self._auth_server()
        url = str(_value(server, "url", "") or "").strip()
        headers = dict(_value(server, "headers", {}) or {})
        if not url:
            raise LarkCLIError("auth MCP URL is not configured", rejected=True)
        headers["Accept"] = "application/json, text/event-stream"
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "auth_access_get",
                "arguments": {
                    "provider": self.settings.provider,
                    "environment": self.settings.environment,
                    "profile": self.settings.auth_profile,
                    "force_refresh": False,
                },
            },
        }
        try:
            response = await self._client().post(url, headers=headers, json=payload, timeout=30)
            response.raise_for_status()
            result = response.json().get("result", {})
        except (httpx.HTTPError, ValueError) as error:
            raise LarkCLIError("auth MCP request failed", retryable=True) from error
        value = result.get("structuredContent")
        if value is None:
            content = result.get("content") or []
            try:
                value = json.loads(content[0]["text"])
            except (IndexError, KeyError, TypeError, ValueError) as error:
                raise LarkCLIError("auth MCP returned invalid data", retryable=True) from error
        if not isinstance(value, dict):
            raise LarkCLIError("auth MCP returned invalid data", retryable=True)
        if value.get("state") != "ready":
            error = value.get("error") or {}
            code = error.get("code") if isinstance(error, dict) else None
            raise LarkCLIError(
                f"auth MCP lark-cli is {value.get('state', 'unavailable')}",
                code=code,
                authentication=True,
            )
        return value

    async def _authorization(self, *, force=False):
        ttl = max(0.0, float(self.settings.token_cache_seconds or 0))
        now = time.monotonic()
        if (
            not force
            and self._auth_cache is not None
            and now - self._auth_cached_at < ttl
        ):
            return self._auth_cache
        value = await self._auth_access()
        if value.get("state") != "ready":
            error = value.get("error") or {}
            code = error.get("code") if isinstance(error, dict) else None
            raise LarkCLIError(
                f"auth MCP lark-cli is {value.get('state', 'unavailable')}",
                code=code,
                authentication=True,
            )
        headers = (value.get("access") or {}).get("headers") or {}
        authorization = headers.get("Authorization") or headers.get("authorization") or ""
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            raise LarkCLIError("auth MCP returned no user token", authentication=True)
        open_id = str(value.get("identity") or "").strip()
        if not open_id:
            raise LarkCLIError("auth MCP returned no user identity", authentication=True)
        expires_at = value.get("expires_at")
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
            expires_at = None
        self._auth_cache = {
            "access_token": authorization.removeprefix("Bearer "),
            "openId": open_id,
            "expires_at": expires_at,
        }
        self._auth_cached_at = time.monotonic()
        self._report("ready", "authorization loaded from auth MCP")
        return self._auth_cache

    async def _command(
        self, *args: str, input_bytes: bytes | None = None, auth: dict | None = None
    ) -> dict:
        argv = [self.settings.executable]
        if self.settings.profile:
            argv.extend(["--profile", self.settings.profile])
        argv.extend(args)
        env = {
            **os.environ,
            "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
            "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
        }
        if auth is not None:
            for key in _PROVIDER_ENV:
                env.pop(key, None)
            env["LARKSUITE_CLI_APP_ID"] = self.app_id
            env["LARKSUITE_CLI_USER_ACCESS_TOKEN"] = auth["access_token"]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE
                if input_bytes is not None
                else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=os.name == "posix",
            )
        except FileNotFoundError:
            raise LarkCLIError("not installed", rejected=True) from None
        except OSError:
            raise LarkCLIError("could not start", rejected=True) from None
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input_bytes)
                if input_bytes is not None
                else process.communicate(),
                timeout=self.settings.timeout_seconds,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError) as error:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                elif process.returncode is None:
                    process.kill()
            except ProcessLookupError:
                pass
            await process.communicate()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise LarkCLIError("timed out", retryable=True) from None
        result = _json_output(stdout)
        if process.returncode != 0 or result.get("ok") is False:
            failure = _json_output(stderr) or result
            error = failure.get("error", {})
            if not isinstance(error, dict):
                error = {}
            raise LarkCLIError(
                "command failed",
                code=error.get("code"),
                authentication=error.get("type") == "authentication" or error.get("code") == 20037,
                retryable=error.get("retryable") is True
                or error.get("type") == "network"
                or error.get("code") == 99991400,
                rejected=error.get("type")
                in (
                    "authentication",
                    "authorization",
                    "permission",
                    "validation",
                    "config",
                )
                or error.get("code") in (230001, 230020, 230027, 230035, 232009, 99991400),
            )
        if not result:
            raise LarkCLIError("returned invalid JSON")
        return result

    async def _user_call(self, auth, *args: str, input_bytes: bytes | None = None) -> dict:
        kwargs = {"input_bytes": input_bytes} if input_bytes is not None else {}
        result = await self._command(
            *args, "--as", "user", "--format", "json", auth=auth, **kwargs
        )
        if result.get("ok") is not True or result.get("identity") != "user":
            raise LarkCLIError("returned invalid user response")
        data = result.get("data")
        if not isinstance(data, dict):
            raise LarkCLIError("returned invalid user data")
        return data

    async def _operate(self, *args, input_bytes=None, before_call=None):
        async with self._lock:
            for attempt in range(2):
                auth = await self._authorization(force=attempt > 0)
                expires_at = auth.get("expires_at")
                if isinstance(expires_at, (int, float)) and expires_at <= time.time() + 5:
                    auth = await self._authorization(force=True)
                timeout = None
                if before_call is not None:
                    timeout = await before_call(auth["openId"])
                expires_at = auth.get("expires_at")
                if isinstance(expires_at, (int, float)) and expires_at <= time.time() + 1:
                    auth = await self._authorization(force=True)
                try:
                    request = self._user_call(auth, *args, input_bytes=input_bytes)
                    if timeout is None:
                        return await request
                    return await asyncio.wait_for(request, timeout)
                except asyncio.TimeoutError:
                    raise LarkCLIError("request budget exhausted", retryable=True) from None
                except LarkCLIError as error:
                    if error.authentication and attempt == 0:
                        self._auth_cache = None
                        continue
                    raise

    async def send_text(self, chat_id, text, idempotency_key, *, before_call=None):
        result = await self._operate(
            "api",
            "POST",
            "/open-apis/im/v1/messages",
            "--params",
            json.dumps({"receive_id_type": "chat_id"}),
            "--data",
            json.dumps(
                {
                    "receive_id": chat_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                    "uuid": idempotency_key,
                },
                ensure_ascii=False,
            ),
            before_call=before_call,
        )
        if not isinstance(result.get("message_id"), str) or not result["message_id"].startswith(
            "om_"
        ):
            raise LarkCLIError("send returned no message receipt", retryable=True)
        return result

    async def message(self, message_id):
        if not message_id.startswith("om_"):
            raise LarkCLIError("invalid IM message identity")
        data = await self._operate(
            "api", "GET", f"/open-apis/im/v1/messages/{quote(message_id, safe='')}"
        )
        for item in data.get("items", []):
            if isinstance(item, dict) and item.get("message_id") == message_id:
                return item
        raise LarkCLIError("message detail missing")

    async def chat_messages(self, chat_id, start, end, page_token=""):
        params = {
            "container_id_type": "chat",
            "container_id": chat_id,
            "start_time": str(int(start)),
            "end_time": str(int(end) + 1),
            "sort_type": "ByCreateTimeAsc",
            "page_size": 50,
        }
        if page_token:
            params["page_token"] = page_token
        return await self._operate(
            "api", "GET", "/open-apis/im/v1/messages", "--params", json.dumps(params)
        )

    async def invite_bot(self, chat_id: str) -> dict | None:
        try:
            return await self._operate(
                "im",
                "chat.members",
                "create",
                "--chat-id",
                chat_id,
                "--member-id-type",
                "app_id",
                "--succeed-type",
                "2",
                "--data",
                json.dumps({"id_list": [self.app_id]}),
            )
        except LarkCLIError as error:
            if error.retryable or not (error.authentication or error.rejected):
                raise
            self._report("cli_error", f"{error}; invitation skipped", warning=True)
            return None

    async def leave_bot(self, chat_id: str) -> bool:
        if not str(chat_id).startswith("oc_"):
            raise LarkCLIError("invalid chat id", rejected=True)
        await self._operate(
            "im",
            "chat.members",
            "delete",
            "--chat-id",
            chat_id,
            "--member-id-type",
            "app_id",
            "--data",
            json.dumps({"id_list": [self.app_id]}),
            "--yes",
        )
        return True

    async def close_ticket(self, ticket_id, authorization, body, *, before_call=None):
        if self.brand is None:
            raise LarkCLIError(
                "Python close requires an official Feishu/Lark domain", rejected=True
            )
        return await self._operate(
            "api",
            "PUT",
            f"/open-apis/helpdesk/v1/tickets/{quote(ticket_id, safe='')}",
            "--data",
            json.dumps(body, ensure_ascii=False),
            before_call=before_call,
        )

    async def close(self):
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
