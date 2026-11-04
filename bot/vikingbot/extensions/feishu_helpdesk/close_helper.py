"""One-shot Python closer; CLI alone owns OAuth renewal and credential writes.

stdin: request JSON line, then a matching commit/abort JSON line.
stdout: versioned ready/result JSON lines. No credential is ever returned.
"""

import argparse
import asyncio
import base64
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

from vikingbot.extensions.feishu_helpdesk.cli_credentials import (
    CredentialError,
    local_session,
    read_access_token,
    refresh_lock,
    require_local_provider,
    verify_profile,
)

PROTOCOL_VERSION = 1
MAX_LINE = 65536
# Helpdesk can also return the platform's generic authentication/scope errors.
# Scope refusals do not invalidate the CLI user's otherwise usable login.
API_AUTHENTICATION_CODES = frozenset((154001, 99991661, 99991663, 99991668, 99991671, 99991677))
API_REJECTION_CODES = API_AUTHENTICATION_CODES | frozenset(
    (
        154000,
        154003,
        154004,
        230027,
        99991400,
        99991662,
        99991672,
        99991673,
        99991676,
        99991679,
    )
)


class CloseFailure(RuntimeError):
    def __init__(self, reason, *, code=None, authentication=False, retryable=False, rejected=True):
        self.reason = reason
        self.code = code if type(code) is int else None
        self.authentication = authentication
        self.retryable = retryable
        self.rejected = rejected
        super().__init__(reason)


def json_output(raw):
    # CLI dry-run/refresh diagnostics can precede a JSON document.
    content = raw.decode("utf-8", errors="replace")
    for offset, character in enumerate(content):
        if character == "{" and (offset == 0 or content[offset - 1] == "\n"):
            try:
                value, _ = json.JSONDecoder().raw_decode(content[offset:])
                if isinstance(value, dict):
                    return value
            except ValueError:
                pass
    raise CloseFailure("CLI returned an unsupported response")


@dataclass(frozen=True)
class CloseRequest:
    request_id: str
    app_id: str
    user_id: str
    brand: str
    ticket_id: str
    executable: str
    profile: str
    authorization: str = field(repr=False)
    body: dict = field(repr=False)
    timeout_seconds: float

    @property
    def path(self):
        return f"/open-apis/helpdesk/v1/tickets/{quote(self.ticket_id, safe='')}"

    @classmethod
    def parse(cls, value):
        try:
            if (
                not isinstance(value, dict)
                or type(value.get("protocol_version")) is not int
                or value["protocol_version"] != PROTOCOL_VERSION
            ):
                raise ValueError()
            for key in (
                "request_id",
                "app_id",
                "user_id",
                "ticket_id",
                "executable",
                "authorization",
            ):
                if (
                    not isinstance(value[key], str)
                    or not value[key]
                    or any(ord(c) < 32 for c in value[key])
                ):
                    raise ValueError()
            if not isinstance(value["profile"], str):
                raise ValueError()
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", value["ticket_id"]):
                raise ValueError()
            if value["brand"] not in ("feishu", "lark"):
                raise ValueError()
            decoded = base64.b64decode(value["authorization"], validate=True).decode()
            if not all(decoded.partition(":")[::2]):
                raise ValueError()
            body, timeout = value["body"], value["timeout_seconds"]
            if (
                not isinstance(body, dict)
                or set(body) - {"status", "solved", "comment"}
                or type(body.get("status")) is not int
                or body["status"] != 51
                or type(body.get("solved")) is not int
                or body["solved"] not in (1, 2)
                or not isinstance(body.get("comment", ""), str)
                or isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout)
                or timeout <= 0
            ):
                raise ValueError()
            return cls(**{key: value[key] for key in cls.__dataclass_fields__})
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise CloseFailure("invalid close request") from None


class CLIProbe:
    """Invoke the official CLI in the helper's process group; capture all output."""

    def __init__(self, request):
        self.request = request
        self.profile = request.profile

    async def run(self, *args):
        argv = [self.request.executable]
        if self.profile:
            argv += ["--profile", self.profile]
        env = {
            **os.environ,
            "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
            "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
        }
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                # The parent reaps the helper's entire group, including npm's
                # CLI launcher and its binary. Do not start a new session here.
            )
        except OSError:
            raise CloseFailure("official CLI could not start") from None
        try:
            stdout, stderr = await process.communicate()
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            raise
        if process.returncode:
            try:
                failure = json_output(stderr or stdout).get("error", {})
            except CloseFailure:
                failure = {}
            if not isinstance(failure, dict):
                failure = {}
            raise CloseFailure(
                "official CLI preparation failed",
                code=failure.get("code"),
                authentication=failure.get("type") == "authentication",
                retryable=failure.get("retryable") is True or failure.get("type") == "network",
            )
        value = json_output(stdout)
        if value.get("ok") is False:
            raise CloseFailure("official CLI preparation was refused")
        return value

    async def prepare(self):
        require_local_provider(os.environ)
        config = await self.run("config", "show")
        # Pin a default profile to its resolved name for every subsequent call.
        self.profile = config.get("profile", "")
        status = await self.run("auth", "status", "--json")
        session = local_session(config, status, self.request.app_id, self.request.user_id)
        if session.brand != self.request.brand or (
            self.request.profile and self.request.profile not in (session.profile, session.app_id)
        ):
            raise CloseFailure("CLI profile or brand does not match this close operation")
        verify_profile(session)
        dry_run = await self.run(
            "api",
            "PUT",
            self.request.path,
            "--as",
            "user",
            "--format",
            "json",
            "--dry-run",
            "--data",
            json.dumps(self.request.body, ensure_ascii=False),
        )
        # v1.0.66 returned a raw diagnostic. Newer CLI releases wrap the
        # identity under data.context. Accept both contracts, but only when
        # the CLI explicitly confirms user identity.
        legacy_identity = (
            dry_run.get("as") == "user"
            and dry_run.get("appId") == session.app_id
            and dry_run.get("userOpenId") == session.user_id
        )
        data = dry_run.get("data")
        context = data.get("context") if isinstance(data, dict) else None
        current_identity = (
            dry_run.get("ok") is True
            and dry_run.get("identity") == "user"
            and isinstance(context, dict)
            and context.get("app_id") == session.app_id
            and context.get("user_open_id") == session.user_id
        )
        if not (legacy_identity or current_identity):
            raise CloseFailure("CLI close policy or identity check failed")
        await self.renew(session)
        return session

    async def renew(self, session):
        info = await self.run(
            "api", "GET", "/open-apis/authen/v1/user_info", "--as", "user", "--format", "json"
        )
        if (
            info.get("ok") is not True
            or info.get("identity") != "user"
            or not isinstance(info.get("data"), dict)
            or info["data"].get("open_id") != session.user_id
        ):
            raise CloseFailure("CLI renewal did not confirm the expected user", authentication=True)
        status = await self.run("auth", "status", "--json")
        config = await self.run("config", "show")
        if local_session(config, status, session.app_id, session.user_id) != session:
            raise CloseFailure("CLI profile changed during renewal")


class CloseOperation:
    def __init__(self, request, receive, emit, *, probe=None, client_factory=None):
        self.request, self.receive, self.emit = request, receive, emit
        self.probe = probe or CLIProbe(request)
        self.client_factory = client_factory or httpx.AsyncClient
        self.deadline = time.monotonic() + request.timeout_seconds
        self.submitted = False

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError()
        return remaining

    def record(self, phase, **fields):
        self.emit(
            {
                "protocol_version": PROTOCOL_VERSION,
                "request_id": self.request.request_id,
                "phase": phase,
                **fields,
            }
        )

    async def run(self):
        try:
            await asyncio.wait_for(self.execute(), self.remaining())
        except CredentialError as error:
            self.result(CloseFailure(str(error), rejected=not self.submitted), kind="credentials")
        except CloseFailure as error:
            self.result(error)
        except asyncio.TimeoutError:
            self.result(
                CloseFailure(
                    "close operation timed out", retryable=True, rejected=not self.submitted
                )
            )
        except Exception:
            # Never emit exception repr/tracebacks: dependencies may include a
            # request, decrypted token or captured CLI output in their messages.
            self.result(CloseFailure("close operation failed", rejected=not self.submitted))

    def result(self, error, *, kind="operation"):
        self.record(
            "result",
            outcome="rejected" if error.rejected else "uncertain",
            error={
                "kind": kind,
                "reason": error.reason,
                "code": error.code,
                "authentication": error.authentication,
                "retryable": error.retryable,
            },
        )

    async def execute(self):
        session = await self.probe.prepare()
        # A concurrent CLI refresh may have replaced the snapshot. Retry only
        # preparation, outside the refresh lock; never retry the actual PUT.
        for attempt in range(2):
            try:
                async with refresh_lock(session, self.deadline):
                    token = read_access_token(session, minimum_validity=self.remaining() + 5)
                    self.remaining()
                    self.record("ready", app_id=session.app_id, user_id=session.user_id)
                    control = await self.receive()
                    if (
                        not isinstance(control, dict)
                        or type(control.get("protocol_version")) is not int
                        or control["protocol_version"] != PROTOCOL_VERSION
                        or control.get("request_id") != self.request.request_id
                        or control.get("action") not in ("commit", "abort")
                    ):
                        raise CloseFailure("invalid close control message")
                    if control["action"] == "abort":
                        outcome = "cancelled"
                    else:
                        # Profile switches/logouts during the parent's final
                        # check must not use a different account after ready.
                        verify_profile(session)
                        if token.expires_at <= time.time() + self.remaining():
                            raise CloseFailure(
                                "CLI user token expired before commit", retryable=True
                            )
                        await self.put(session.brand, token.value)
                        outcome = "accepted"
                # Emit exactly one result, after HTTP/lock cleanup also succeeds.
                self.record("result", outcome=outcome)
                return
            except CredentialError as error:
                if str(error) != "CLI user token needs renewal" or attempt:
                    raise
            await self.probe.renew(session)

    async def put(self, brand, access_token):
        origin = "https://open.feishu.cn" if brand == "feishu" else "https://open.larksuite.com"
        async with self.client_factory(timeout=self.remaining(), follow_redirects=False) as client:
            self.submitted = True
            try:
                response = await client.put(
                    origin + self.request.path,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "X-Lark-Helpdesk-Authorization": self.request.authorization,
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    json=self.request.body,
                )
            except httpx.HTTPError:
                raise CloseFailure(
                    "close response was not received", retryable=True, rejected=False
                ) from None
            try:
                value = response.json()
                code = value.get("code") if isinstance(value, dict) else None
                if type(code) is not int:
                    raise ValueError()
            except (ValueError, TypeError):
                raise CloseFailure("close returned an invalid response", rejected=False) from None
            # Native PUT close responses may omit or null data on success.
            # A 2xx response with the native API's code=0 is authoritative.
            if 200 <= response.status_code < 300 and code == 0:
                return
            # Only explicit API refusals prove that this PUT did not close.
            rejected = code in API_REJECTION_CODES and response.status_code < 500
            raise CloseFailure(
                "close API refused the request" if rejected else "close result is uncertain",
                code=code,
                authentication=rejected and code in API_AUTHENTICATION_CODES,
                retryable=code == 99991400 or response.status_code >= 500,
                rejected=rejected,
            )


async def main():
    reader = asyncio.StreamReader(limit=MAX_LINE)
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: protocol, sys.stdin.buffer
    )

    async def receive():
        line = await reader.readline()
        if not line or len(line) > MAX_LINE:
            raise CloseFailure("close input was interrupted")
        try:
            return json.loads(line)
        except (ValueError, UnicodeError):
            raise CloseFailure("invalid close input") from None

    def emit(record):
        print(json.dumps(record, ensure_ascii=False), flush=True)

    try:
        request = CloseRequest.parse(await receive())
        await CloseOperation(request, receive, emit).run()
    except Exception:
        # Invalid requests have no trusted request identity. The parent treats
        # this as a pre-commit failure, and never receives any input echoed back.
        emit({"protocol_version": PROTOCOL_VERSION, "phase": "result", "outcome": "rejected"})
    finally:
        transport.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VikingBot Python Helpdesk closer")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--json", action="store_true")
    options = parser.parse_args()
    if options.version:
        print(json.dumps({"protocol_version": PROTOCOL_VERSION, "implementation": "python"}))
    else:
        asyncio.run(main())
