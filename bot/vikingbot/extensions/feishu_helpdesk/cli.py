"""User operations and idle-time renewal, with lark-cli owning all user credentials."""

import asyncio
import json
import os
import signal
import time

from loguru import logger

from vikingbot.extensions.feishu_helpdesk.config import LarkCLIConfig


class LarkCLIError(RuntimeError):
    """Never include CLI stdout, stderr, request data or credentials in diagnostics."""

    def __init__(self, reason: str, *, code=None, authentication=False, retryable=False):
        self.code = code if isinstance(code, int) else None
        self.authentication = authentication
        self.retryable = retryable and not authentication
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
    def __init__(self, settings: LarkCLIConfig, app_id: str):
        self.settings = settings
        self.app_id = app_id
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

    async def _command(self, *args: str) -> dict:
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
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                # npm's launcher spawns a child binary; cancel the whole private group.
                start_new_session=os.name == "posix",
            )
        except FileNotFoundError:
            raise LarkCLIError("not installed") from None
        except OSError:
            raise LarkCLIError("could not start") from None
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.settings.timeout_seconds
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

    async def _user_call(self, *args: str) -> dict:
        result = await self._command(*args, "--as", "user", "--format", "json")
        if result.get("ok") is not True or result.get("identity") != "user":
            raise LarkCLIError("returned invalid user response")
        data = result.get("data")
        if not isinstance(data, dict):
            raise LarkCLIError("returned invalid user data")
        return data

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
