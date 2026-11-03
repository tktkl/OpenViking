"""Read-only compatibility adapter for lark-cli's local profile credentials.

The storage contract is based on larksuite/cli 4e2cbea (v1.0.66). Compatibility
is checked structurally, rather than rejecting every newer CLI version. Only
the CLI writes credentials and refreshes tokens; this module never does either.
"""

import asyncio
import base64
import binascii
import fcntl
import json
import math
import os
import re
import stat
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CredentialError(RuntimeError):
    """Contains a fixed diagnostic only, never secret-store or CLI output."""


@dataclass(frozen=True)
class LocalSession:
    app_id: str
    user_id: str
    profile: str
    workspace: str
    brand: str


@dataclass(frozen=True)
class AccessToken:
    value: str = field(repr=False)
    expires_at: float


WORKSPACES = {"local", "openclaw", "hermes", "lark-channel"}
SAFE_ID = re.compile(r"[^a-zA-Z0-9._-]")
# These select credential providers ahead of the persisted profile. Never read a
# different local account just because an injected provider used the same app.
PROVIDER_ENV = (
    "LARKSUITE_CLI_APP_ID",
    "LARKSUITE_CLI_APP_SECRET",
    "LARKSUITE_CLI_USER_ACCESS_TOKEN",
    "LARKSUITE_CLI_TENANT_ACCESS_TOKEN",
    "LARKSUITE_CLI_AUTH_PROXY",
)


def require_local_provider(env):
    if any(env.get(key) for key in PROVIDER_ENV):
        raise CredentialError("unsupported credential provider; use a local CLI profile")


def local_session(config, status, app_id, user_id):
    """Resolve identity from public CLI diagnostics, without reading AppSecret."""
    if not isinstance(config, dict) or not isinstance(status, dict):
        raise CredentialError("unsupported CLI profile diagnostics")
    identities = status.get("identities")
    user = identities.get("user") if isinstance(identities, dict) else None
    if not isinstance(user, dict):
        raise CredentialError("unsupported CLI profile diagnostics")
    # External providers report ready rather than the local store's valid /
    # needs_refresh token status. Do not fall back to a stale local account.
    if user.get("tokenStatus") == "ready":
        raise CredentialError("unsupported credential provider; use a local CLI profile")
    if (
        config.get("appId") != app_id
        or status.get("appId") != app_id
        or user.get("openId") != user_id
        or user.get("available") is not True
        or user.get("status") not in ("ready", "needs_refresh")
        or user.get("tokenStatus") not in ("valid", "needs_refresh")
    ):
        raise CredentialError("CLI profile or user does not match this close operation")
    profile, workspace, brand = (config.get(key) for key in ("profile", "workspace", "brand"))
    if (
        not isinstance(profile, str)
        or not profile
        or not isinstance(workspace, str)
        or workspace not in WORKSPACES
        or brand not in ("feishu", "lark")
        or status.get("brand") != brand
    ):
        raise CredentialError("unsupported CLI profile diagnostics")
    return LocalSession(app_id, user_id, profile, workspace, brand)


def runtime_dir(session, *, env=None, home=None):
    env = os.environ if env is None else env
    home = Path.home() if home is None else Path(home)
    base = Path(env.get("LARKSUITE_CLI_CONFIG_DIR") or home / ".lark-cli")
    return base if session.workspace == "local" else base / session.workspace


def storage_dir(*, platform=None, env=None, home=None):
    platform = sys.platform if platform is None else platform
    env = os.environ if env is None else env
    home = Path.home() if home is None else Path(home)
    if platform == "darwin":
        return home / "Library" / "Application Support" / "lark-cli"
    if platform == "linux":
        override = env.get("LARKSUITE_CLI_DATA_DIR", "")
        # CLI ignores invalid data-directory overrides and uses its default.
        # resolve() matches its symlink/nearest-existing-ancestor normalization.
        if (
            override
            and Path(override).is_absolute()
            and not any(ord(c) < 32 or ord(c) == 127 for c in override)
        ):
            try:
                base = Path(override).resolve()
            except (OSError, RuntimeError):
                raise CredentialError("CLI credential storage is unavailable") from None
        else:
            base = home / ".local" / "share"
        return base / "lark-cli"
    raise CredentialError("local CLI credential reading requires macOS or Linux")


def _read(path, limit=65536):
    try:
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise CredentialError("unsupported CLI credential size")
        return data
    except OSError:
        raise CredentialError("CLI credential storage is unavailable") from None


def verify_profile(session, *, env=None, home=None):
    """Fail closed if login/profile selection changed since CLI preparation."""
    try:
        config = json.loads(_read(runtime_dir(session, env=env, home=home) / "config.json"))
        apps = config["apps"]
        if not isinstance(apps, list) or not all(isinstance(app, dict) for app in apps):
            raise ValueError()
        app = next((a for a in apps if a.get("name") == session.profile), None)
        if app is None:
            app = next((a for a in apps if a.get("appId") == session.profile), None)
        if (
            app is None
            or app.get("appId") != session.app_id
            or app.get("brand") != session.brand
            or app["users"][0]["userOpenId"] != session.user_id
        ):
            raise ValueError()
    except (ValueError, KeyError, TypeError, IndexError, UnicodeError):
        raise CredentialError("CLI profile changed or has an unsupported format") from None


def _keychain_key(timeout):
    try:
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                "lark-cli",
                "-a",
                "master.key",
                "-w",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=min(5, timeout),
            check=False,
        )
        if result.returncode:
            raise ValueError()
        key = base64.b64decode(result.stdout.strip(), validate=True)
        if len(key) != 32:
            raise ValueError()
        return key
    except (OSError, ValueError, binascii.Error, subprocess.TimeoutExpired):
        raise CredentialError("CLI Keychain master key is unavailable") from None


def _decrypt(data, key):
    if len(key) != 32 or len(data) < 28:
        raise ValueError()
    return AESGCM(key).decrypt(data[:12], data[12:], None)


def read_access_token(session, *, minimum_validity, platform=None, env=None, home=None):
    """Read the current UAT; never return or persist the refresh token."""
    platform = sys.platform if platform is None else platform
    env = os.environ if env is None else env
    require_local_provider(env)
    verify_profile(session, env=env, home=home)
    directory = storage_dir(platform=platform, env=env, home=home)
    filename = SAFE_ID.sub("_", f"{session.app_id}:{session.user_id}") + ".enc"
    data = _read(directory / filename)
    try:
        if platform == "darwin":
            # Match CLI file-first reads. Do not create or downgrade a master key.
            try:
                plaintext = _decrypt(data, _read(directory / "master.key.file", 32))
            except (CredentialError, ValueError, InvalidTag):
                plaintext = _decrypt(data, _keychain_key(minimum_validity))
        else:
            plaintext = _decrypt(data, _read(directory / "master.key", 32))
        stored = json.loads(plaintext)
        value, expires = stored["accessToken"], stored["expiresAt"]
        if (
            stored["appId"] != session.app_id
            or stored["userOpenId"] != session.user_id
            or not isinstance(value, str)
            or not value
            or not value.isascii()
            or any(ord(character) <= 32 or ord(character) >= 127 for character in value)
            or isinstance(expires, bool)
            or not isinstance(expires, (int, float))
            or not math.isfinite(expires)
        ):
            raise ValueError()
        if (
            not isinstance(stored.get("scope"), str)
            or "helpdesk:all" not in stored["scope"].split()
        ):
            raise CredentialError("CLI user has not authorized helpdesk:all")
        expires /= 1000
        if expires <= time.time() + minimum_validity:
            raise CredentialError("CLI user token needs renewal")
        return AccessToken(value, expires)
    except (ValueError, KeyError, TypeError, UnicodeError, InvalidTag):
        raise CredentialError("unsupported or invalid CLI credential format") from None


@asynccontextmanager
async def refresh_lock(session, deadline, *, env=None, home=None):
    """Same POSIX flock and filename as CLI's refreshWithLock, with a deadline."""
    directory = runtime_dir(session, env=env, home=home) / "locks"
    path = directory / (
        f"refresh_{SAFE_ID.sub('_', session.app_id)}_{SAFE_ID.sub('_', session.user_id)}.lock"
    )
    fd = None
    try:
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise CredentialError("CLI refresh lock is not a regular file")
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CredentialError("CLI refresh lock timed out") from None
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(min(0.05, remaining))
        except OSError:
            raise CredentialError("CLI refresh lock is unavailable") from None
        yield
    finally:
        if fd is not None:
            os.close(fd)
