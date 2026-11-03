"""Read-only compatibility with the CLI's actual AES-GCM and flock contract."""

import asyncio
import base64
import fcntl
import json
import os
import time
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from vikingbot.extensions.feishu_helpdesk import cli_credentials as credentials


@pytest.fixture
def local_store(tmp_path):
    session = credentials.LocalSession("cli_bot", "ou_agent", "support", "local", "feishu")
    env = {"LARKSUITE_CLI_CONFIG_DIR": str(tmp_path / "config")}
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
    runtime = credentials.runtime_dir(session, env=env, home=tmp_path)
    runtime.mkdir(parents=True)
    (runtime / "config.json").write_text(json.dumps(config))
    stored = {
        "appId": "cli_bot",
        "userOpenId": "ou_agent",
        "accessToken": "fake-access-token",
        "refreshToken": "fake-refresh-token",
        "expiresAt": int((time.time() + 3600) * 1000),
        "scope": "offline_access helpdesk:all im:message",
    }
    key = AESGCM.generate_key(bit_length=256)

    def write(*, platform="linux", values=None, master="file"):
        directory = credentials.storage_dir(platform=platform, env=env, home=tmp_path)
        directory.mkdir(parents=True, exist_ok=True)
        if master == "file":
            (directory / ("master.key.file" if platform == "darwin" else "master.key")).write_bytes(
                key
            )
        nonce = bytes(range(12))
        data = nonce + AESGCM(key).encrypt(nonce, json.dumps(values or stored).encode(), None)
        path = directory / "cli_bot_ou_agent.enc"
        path.write_bytes(data)
        return directory, path

    def read(**kwargs):
        return credentials.read_access_token(
            session, minimum_validity=30, env=env, home=tmp_path, **kwargs
        )

    return SimpleNamespace(
        session=session,
        env=env,
        home=tmp_path,
        runtime=runtime,
        config=config,
        stored=stored,
        key=key,
        write=write,
        read=read,
    )


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_reads_existing_store_without_writing_or_exposing_credentials(local_store, platform):
    store = local_store
    store.write(platform=platform)
    before = {str(p): p.read_bytes() for p in store.home.rglob("*") if p.is_file()}
    token = store.read(platform=platform)
    assert token.value == "fake-access-token"
    assert "fake-access-token" not in repr(token)
    assert "refresh" not in repr(token)
    assert before == {str(p): p.read_bytes() for p in store.home.rglob("*") if p.is_file()}


def test_linux_data_directory_and_workspace_match_cli(local_store):
    store = local_store
    store.env["LARKSUITE_CLI_DATA_DIR"] = str(store.home / "custom-data")
    directory, _ = store.write()
    assert directory == store.home / "custom-data" / "lark-cli"
    session = credentials.LocalSession("cli_bot", "ou_agent", "support", "hermes", "feishu")
    assert credentials.runtime_dir(session, env=store.env) == store.runtime / "hermes"


@pytest.mark.parametrize("override", ["relative-data", "/tmp/bad\npath"])
def test_linux_invalid_data_override_uses_the_cli_default(local_store, override):
    store = local_store
    store.env["LARKSUITE_CLI_DATA_DIR"] = override
    directory, _ = store.write()
    assert directory == store.home / ".local" / "share" / "lark-cli"
    assert store.read(platform="linux").value == "fake-access-token"


def test_different_workspace_never_falls_back_to_local_profile(local_store):
    store = local_store
    store.write()
    other = credentials.LocalSession("cli_bot", "ou_agent", "support", "hermes", "feishu")
    with pytest.raises(credentials.CredentialError, match="storage is unavailable"):
        credentials.read_access_token(
            other, minimum_validity=30, platform="linux", env=store.env, home=store.home
        )


@pytest.mark.parametrize("status", [None, [], {"identities": None}, {"identities": {"user": []}}])
def test_unknown_cli_identity_diagnostics_fail_closed(status):
    with pytest.raises(credentials.CredentialError, match="unsupported CLI profile diagnostics"):
        credentials.local_session({}, status, "cli_bot", "ou_agent")


def test_custom_credential_provider_is_not_treated_as_local_login():
    with pytest.raises(credentials.CredentialError, match="unsupported credential provider"):
        credentials.local_session(
            {}, {"identities": {"user": {"tokenStatus": "ready"}}}, "cli_bot", "ou_agent"
        )


def test_macos_reads_keychain_only_after_file_key_cannot_decrypt(local_store, monkeypatch):
    store = local_store
    directory, _ = store.write(platform="darwin", master="keychain")
    calls = []

    def security(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=base64.b64encode(store.key) + b"\n")

    monkeypatch.setattr(credentials.subprocess, "run", security)
    assert store.read(platform="darwin").value == "fake-access-token"
    assert calls[0][0] == [
        "/usr/bin/security",
        "find-generic-password",
        "-s",
        "lark-cli",
        "-a",
        "master.key",
        "-w",
    ]
    assert not (directory / "master.key.file").exists()
    (directory / "master.key.file").write_bytes(b"x" * 32)
    assert store.read(platform="darwin").value == "fake-access-token"
    assert len(calls) == 2


def test_macos_file_key_never_needs_system_keychain(local_store, monkeypatch):
    local_store.write(platform="darwin")
    monkeypatch.setattr(
        credentials.subprocess, "run", lambda *a, **kw: pytest.fail("Keychain read")
    )
    assert local_store.read(platform="darwin").value == "fake-access-token"


def test_keychain_denial_is_sanitized_and_does_not_downgrade(local_store, monkeypatch):
    directory, _ = local_store.write(platform="darwin", master="keychain")
    monkeypatch.setattr(
        credentials.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=1, stdout=b"fake-secret", stderr=b"fake-secret"
        ),
    )
    with pytest.raises(
        credentials.CredentialError, match="Keychain master key is unavailable"
    ) as error:
        local_store.read(platform="darwin")
    assert "fake-secret" not in str(error.value)
    assert sorted(p.name for p in directory.iterdir()) == ["cli_bot_ou_agent.enc"]


@pytest.mark.parametrize(
    "change",
    [
        {"appId": "other"},
        {"userOpenId": "other"},
        {"accessToken": ""},
        {"expiresAt": True},
        {"expiresAt": "tomorrow"},
        {"scope": "helpdesk:all:readonly"},
        {"expiresAt": 1},
        {"accessToken": "fake\r\nsecret"},
        {"accessToken": "fake\x00secret"},
        {"accessToken": "fake secret"},
        {"accessToken": "未知格式"},
    ],
)
def test_unknown_wrong_account_and_expired_credentials_fail_closed(local_store, change):
    local_store.write(values={**local_store.stored, **change})
    with pytest.raises(credentials.CredentialError) as error:
        local_store.read(platform="linux")
    assert "fake-access-token" not in str(error.value)
    assert "fake-refresh-token" not in str(error.value)


@pytest.mark.parametrize("damage", [b"short", b"x" * 100])
def test_damaged_ciphertext_is_rejected(local_store, damage):
    _, path = local_store.write()
    path.write_bytes(damage)
    with pytest.raises(credentials.CredentialError, match="credential format"):
        local_store.read(platform="linux")


@pytest.mark.parametrize("key", credentials.PROVIDER_ENV)
def test_environment_provider_does_not_fall_back_to_unrelated_local_token(local_store, key):
    local_store.write()
    local_store.env[key] = "fake-secret"
    with pytest.raises(
        credentials.CredentialError, match="unsupported credential provider"
    ) as error:
        local_store.read(platform="linux")
    assert "fake-secret" not in str(error.value)


def test_switching_logged_in_user_invalidates_prepared_session(local_store):
    store = local_store
    store.write()
    store.config["apps"][0]["users"][0]["userOpenId"] = "ou_other"
    (store.runtime / "config.json").write_text(json.dumps(store.config))
    with pytest.raises(credentials.CredentialError, match="profile changed"):
        store.read(platform="linux")


@pytest.mark.asyncio
async def test_refresh_lock_waits_for_cli_compatible_flock_and_releases_on_cancel(local_store):
    store = local_store
    directory = store.runtime / "locks"
    directory.mkdir()
    path = directory / "refresh_cli_bot_ou_agent.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    entered = asyncio.Event()

    async def hold():
        async with credentials.refresh_lock(
            store.session, time.monotonic() + 3, env=store.env, home=store.home
        ):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(hold())
    await asyncio.sleep(0.06)
    assert not entered.is_set()
    os.close(fd)
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    async with credentials.refresh_lock(
        store.session, time.monotonic() + 1, env=store.env, home=store.home
    ):
        assert path.exists()


@pytest.mark.asyncio
async def test_refresh_lock_timeout_is_bounded(local_store):
    store = local_store
    async with credentials.refresh_lock(store.session, time.monotonic() + 1, env=store.env):
        with pytest.raises(credentials.CredentialError, match="timed out"):
            async with credentials.refresh_lock(
                store.session, time.monotonic() + 0.05, env=store.env
            ):
                pytest.fail("acquired a lock still held by the CLI")


@pytest.mark.asyncio
async def test_refresh_lock_does_not_relabel_errors_after_submission(local_store):
    store = local_store
    with pytest.raises(OSError, match="HTTP cleanup"):
        async with credentials.refresh_lock(store.session, time.monotonic() + 1, env=store.env):
            raise OSError("HTTP cleanup")
    # The operation's failure still releases the cross-process lock.
    async with credentials.refresh_lock(store.session, time.monotonic() + 1, env=store.env):
        pass
