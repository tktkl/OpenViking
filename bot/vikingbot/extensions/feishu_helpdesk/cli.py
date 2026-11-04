"""Official CLI user operations and a two-phase Python Helpdesk closer."""

import asyncio
import json
import os
import signal
import sys
import time
from urllib.parse import quote
from uuid import uuid4

from loguru import logger

from vikingbot.extensions.feishu_helpdesk.config import LarkCLIConfig

CLOSE_OPERATION_DIAGNOSTICS = frozenset(
    {
        "CLI returned an unsupported response",
        "invalid close request",
        "CLI profile or brand does not match this close operation",
        "CLI close policy or identity check failed",
        "CLI renewal did not confirm the expected user",
        "CLI profile changed during renewal",
        "close operation timed out",
        "close operation failed",
        "CLI user token expired before commit",
        "close response was not received",
        "close returned an invalid response",
        "close API refused the request",
        "close result is uncertain",
        "close input was interrupted",
        "invalid close input",
    }
)
CLOSE_CREDENTIAL_DIAGNOSTICS = frozenset(
    {
        "unsupported credential provider; use a local CLI profile",
        "CLI profile or user does not match this close operation",
        "unsupported CLI profile diagnostics",
        "local CLI credential reading requires macOS or Linux",
        "unsupported CLI credential size",
        "CLI credential storage is unavailable",
        "CLI profile changed or has an unsupported format",
        "CLI Keychain master key is unavailable",
        "CLI user has not authorized helpdesk:all",
        "CLI user token needs renewal",
        "unsupported or invalid CLI credential format",
        "CLI refresh lock is not a regular file",
        "CLI refresh lock timed out",
        "CLI refresh lock is unavailable",
    }
)


class LarkCLIError(RuntimeError):
    """Never include CLI stdout, stderr, request data or credentials in diagnostics."""

    def __init__(
        self, reason: str, *, code=None, authentication=False, retryable=False, rejected=False
    ):
        self.code = code if isinstance(code, int) else None
        self.authentication = authentication
        self.retryable = retryable and not authentication
        # This describes only the current attempt, never any earlier retry.
        self.rejected = rejected
        super().__init__(f"Feishu Helpdesk lark-cli {reason} (code={self.code})")


def _json_output(raw: bytes) -> dict:
    # Refresh diagnostics can precede the structured error on stderr.
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


class LarkCLIAdapter:
    def __init__(self, settings: LarkCLIConfig, app_id: str, *, domain="https://open.feishu.cn"):
        self.settings = settings
        self.app_id = app_id
        self.brand = {
            "https://open.feishu.cn": "feishu",
            "https://open.larksuite.com": "lark",
        }.get(domain.rstrip("/"))
        self._lock = asyncio.Lock()
        self._keepalive_task: asyncio.Task | None = None
        self._blocked_authorization: tuple | None = None
        self._notice = ""
        self._next_keepalive = 0.0
        self._retry_at = 0.0
        self._failures = 0

    def _report(self, state: str, message: str, *, warning=False):
        if state != self._notice:
            logger.log("WARNING" if warning else "INFO", "Feishu Helpdesk CLI: {}", message)
            self._notice = state

    async def _command(self, *args: str, input_bytes: bytes | None = None) -> dict:
        argv = [self.settings.executable]
        if self.settings.profile:
            argv.extend(["--profile", self.settings.profile])
        argv.extend(args)
        env = {
            **os.environ,
            "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
            "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
        }
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE
                if input_bytes is not None
                else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                # npm's launcher spawns a child binary; cancel the whole private group.
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
                # Only known refusals prove that this attempt did not send.
                # Unknown API/server/transport failures keep an uncertain receipt.
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

    @staticmethod
    def _signature(user: dict) -> tuple:
        return tuple(
            user.get(key)
            for key in ("openId", "grantedAt", "expiresAt", "refreshExpiresAt", "scope")
        )

    async def _authorization(self) -> dict | None:
        # No --verify: this gate only inspects local credentials, never sends user_info.
        status = await self._command("auth", "status", "--json")
        if status.get("appId") != self.app_id:
            self._report(
                "wrong_app", "App ID does not match this bot; user operations paused", warning=True
            )
            return None
        identities = status.get("identities", {})
        user = identities.get("user", {}) if isinstance(identities, dict) else {}
        if (
            not isinstance(user, dict)
            or user.get("available") is not True
            or not user.get("openId")
            or user.get("status") not in ("ready", "needs_refresh")
            or user.get("tokenStatus") not in ("valid", "needs_refresh")
        ):
            self._next_keepalive = 0
            self._report("signed_out", "no usable user login; skipping invitations and keepalive")
            return None
        if self._signature(user) == self._blocked_authorization:
            return None
        return user

    async def _user_call(self, *args: str, input_bytes: bytes | None = None) -> dict:
        kwargs = {"input_bytes": input_bytes} if input_bytes is not None else {}
        result = await self._command(*args, "--as", "user", "--format", "json", **kwargs)
        if result.get("ok") is not True or result.get("identity") != "user":
            raise LarkCLIError("returned invalid user response")
        data = result.get("data")
        if not isinstance(data, dict):
            raise LarkCLIError("returned invalid user data")
        return data

    async def _operate(self, *args, input_bytes=None, before_call=None):
        async with self._lock:
            user = await self._authorization()
            if user is None:
                raise LarkCLIError("no usable user authorization", authentication=True)
            timeout = None
            if before_call is not None:
                # Send guards may return a shorter request budget after lock/auth waits.
                timeout = await before_call(user["openId"])
            try:
                request = self._user_call(*args, input_bytes=input_bytes)
                if timeout is None:
                    return await request
                # Include CLI startup, its credential refresh and the HTTP call.
                # Cancellation reaps the private process group in _command().
                return await asyncio.wait_for(request, timeout)
            except asyncio.TimeoutError:
                raise LarkCLIError("request budget exhausted", retryable=True) from None
            except LarkCLIError as error:
                if error.authentication:
                    self._authorization_failed(user)
                raise

    async def send_text(self, chat_id, text, idempotency_key, *, before_call=None):
        # Raw API output preserves Feishu's millisecond create_time. The send
        # shortcut formats it as local wall time and loses the original epoch.
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

    async def close_ticket(self, ticket_id, authorization, body, *, before_call=None):
        if self.brand is None:
            raise LarkCLIError(
                "Python close requires an official Feishu/Lark domain", rejected=True
            )
        deadline = time.monotonic() + self.settings.timeout_seconds
        submission = {"committed": False}

        async def close():
            # Include lock/auth waits in the total operation budget. The helper
            # then completes renewal before asking for the final business guard.
            async with self._lock:
                user = await self._authorization()
                if user is None:
                    raise LarkCLIError(
                        "no usable user authorization", authentication=True, rejected=True
                    )
                try:
                    return await self._close_helper(
                        ticket_id,
                        authorization,
                        body,
                        user["openId"],
                        before_call,
                        deadline,
                        submission,
                    )
                except LarkCLIError as error:
                    if error.authentication:
                        self._authorization_failed(user)
                    raise

        try:
            return await asyncio.wait_for(close(), self.settings.timeout_seconds)
        except asyncio.TimeoutError:
            raise LarkCLIError(
                "Python close timed out", retryable=True, rejected=not submission["committed"]
            ) from None

    async def _close_helper(
        self, ticket_id, authorization, body, user_id, before_call, deadline, submission
    ):
        request_id = str(uuid4())
        request = {
            "protocol_version": 1,
            "request_id": request_id,
            "app_id": self.app_id,
            "brand": self.brand,
            "user_id": user_id,
            "ticket_id": ticket_id,
            "executable": self.settings.executable,
            "profile": self.settings.profile,
            "authorization": authorization,
            "body": body,
            "timeout_seconds": max(0.001, deadline - time.monotonic()),
        }
        process, stderr_task, committed = None, None, False

        async def discard(stream):
            while await stream.read(8192):
                pass

        async def receive():
            try:
                value = json.loads(await process.stdout.readline())
                if (
                    not isinstance(value, dict)
                    or type(value.get("protocol_version")) is not int
                    or value["protocol_version"] != 1
                    or value.get("request_id") != request_id
                ):
                    raise ValueError()
                return value
            except (ValueError, UnicodeError):
                raise LarkCLIError(
                    "invalid Python close helper response", rejected=not committed
                ) from None

        def check_result(value):
            if value.get("phase") != "result":
                raise LarkCLIError("invalid Python close helper phase", rejected=not committed)
            outcome = value.get("outcome")
            if outcome == "accepted" and committed:
                return {}
            error = value.get("error", {})
            if not isinstance(error, dict):
                error = {}
            # Never display arbitrary helper/CLI output. These fixed adapter
            # diagnostics make storage incompatibility actionable without
            # exposing decrypted data, paths or captured process output.
            reason = "Python close helper failed"
            detail = error.get("reason")
            if error.get("kind") == "credentials":
                reason = "Python close cannot read compatible local CLI credentials"
                if isinstance(detail, str) and detail in CLOSE_CREDENTIAL_DIAGNOSTICS:
                    reason += f": {detail}"
            elif isinstance(detail, str) and detail in CLOSE_OPERATION_DIAGNOSTICS:
                reason += f": {detail}"
            raise LarkCLIError(
                reason,
                code=error.get("code"),
                authentication=error.get("authentication") is True,
                retryable=error.get("retryable") is True,
                rejected=not committed or outcome in ("rejected", "cancelled"),
            )

        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "vikingbot.extensions.feishu_helpdesk.close_helper",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
            stderr_task = asyncio.create_task(discard(process.stderr))
            process.stdin.write(json.dumps(request, ensure_ascii=False).encode() + b"\n")
            await process.stdin.drain()
            ready = await receive()
            if ready.get("phase") != "ready":
                return check_result(ready)
            if ready.get("app_id") != self.app_id or ready.get("user_id") != user_id:
                raise LarkCLIError("Python close helper identity mismatch", rejected=True)
            if before_call is not None:
                await before_call(user_id)
            # Persisting close_pending happens in before_call, immediately
            # before this commit. No await separates the guard from the write.
            committed = True
            submission["committed"] = True
            process.stdin.write(
                json.dumps(
                    {"protocol_version": 1, "request_id": request_id, "action": "commit"}
                ).encode()
                + b"\n"
            )
            await process.stdin.drain()
            result = check_result(await receive())
            # Process.wait() can also wait for inherited pipe EOF when a CLI
            # descendant outlives the leader. Observe the leader's exit first;
            # finally then kills the group and drains every remaining pipe.
            while process.returncode is None:
                await asyncio.sleep(0.01)
            if process.returncode != 0:
                raise LarkCLIError("Python close helper exited unexpectedly")
            return result
        except OSError:
            raise LarkCLIError(
                "Python close helper communication failed", rejected=not committed
            ) from None
        finally:
            if process is not None:
                if not committed and process.stdin is not None:
                    try:
                        process.stdin.write(
                            json.dumps(
                                {"protocol_version": 1, "request_id": request_id, "action": "abort"}
                            ).encode()
                            + b"\n"
                        )
                    except (OSError, RuntimeError):
                        pass
                # Reap the complete group even when the helper already exited:
                # a timed-out npm launcher may have left a CLI binary behind.
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    elif process.returncode is None:
                        process.kill()
                except ProcessLookupError:
                    pass
                stdout_task = asyncio.create_task(discard(process.stdout))
                await process.wait()
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

    def _authorization_failed(self, user: dict):
        self._blocked_authorization = self._signature(user)
        self._next_keepalive = 0
        self._report(
            "authorization_expired", "user authorization failed; please log in again", warning=True
        )

    async def invite_bot(self, chat_id: str) -> dict | None:
        async with self._lock:
            try:
                user = await self._authorization()
            except LarkCLIError as error:
                if error.retryable:
                    raise
                # Missing CLI/configuration must not raise on every ticket event.
                self._report("cli_error", str(error) + "; invitation skipped", warning=True)
                return None
            if user is None:
                return None
            try:
                return await self._user_call(
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
                if not error.authentication:
                    raise
                self._authorization_failed(user)
                return None

    async def keepalive_once(self):
        async with self._lock:
            user = None
            try:
                user = await self._authorization()
                if user is None:
                    return
                now = time.monotonic()
                if now < self._retry_at:
                    return
                if now < self._next_keepalive and user["tokenStatus"] != "needs_refresh":
                    return
                await self._user_call("api", "GET", "/open-apis/authen/v1/user_info")
            except LarkCLIError as error:
                if error.authentication and user is not None:
                    self._authorization_failed(user)
                else:
                    self._failures = min(self._failures + 1, 5)
                    self._retry_at = time.monotonic() + min(30 * 2 ** (self._failures - 1), 300)
                    self._report(
                        "cli_error", str(error) + "; will retry with backoff", warning=True
                    )
                return
            self._failures = 0
            self._retry_at = 0
            self._next_keepalive = time.monotonic() + self.settings.keepalive_interval_seconds
            self._report(
                "ready", "authorized user keepalive active; credentials managed by lark-cli"
            )

    def start(self):
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = asyncio.create_task(
                self._keepalive_loop(), name="feishu-helpdesk-cli-keepalive"
            )

    async def _keepalive_loop(self):
        while True:
            await self.keepalive_once()
            await asyncio.sleep(
                min(
                    self.settings.auth_check_interval_seconds,
                    self.settings.keepalive_interval_seconds,
                )
            )

    async def close(self):
        task, self._keepalive_task = self._keepalive_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
