"""Resolve Feishu Web internal feed ids without interactive browser use.

Feishu's OpenAPI exposes ``oc_*`` chat ids, while the Web Messenger gateway
uses a different numeric feed id.  The mapping is not returned by Helpdesk or
OpenAPI.  This module keeps a small on-disk cache and, when a new ticket needs
one, resolves it with a short-lived headless Chrome process (no visible UI).

The browser is deliberately confined to this adapter: once the mapping is
cached, message delivery is plain HTTP and does not launch a browser.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import glob
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import httpx


class FeedResolutionError(RuntimeError):
    """A safe failure message; cookies and page content are never included."""


def _normal_name(value: str) -> str:
    return re.sub(r"\s+", "", value or "").casefold()


def _cookie_signature(cookies: list[dict]) -> tuple:
    return tuple(
        sorted(
            (
                str(cookie.get("name", "")),
                str(cookie.get("value", "")),
                str(cookie.get("domain", "")),
                str(cookie.get("path", "/")),
            )
            for cookie in cookies
            if isinstance(cookie, dict)
        )
    )


def _chat_names(chat: dict) -> list[str]:
    names: list[str] = []

    def add(value):
        if isinstance(value, str) and value.strip() and value.strip() not in names:
            names.append(value.strip())

    add(chat.get("name"))
    translations = chat.get("i18n_names")
    if isinstance(translations, dict):
        for value in translations.values():
            add(value)
    return names


def _find_chrome(configured: str = "") -> str:
    candidates = [configured] if configured else []
    candidates.extend(
        [
            shutil.which("google-chrome-stable") or "",
            shutil.which("google-chrome") or "",
            shutil.which("chromium") or "",
            shutil.which("chromium-browser") or "",
        ]
    )
    candidates.extend(
        sorted(
            glob.glob(
                str(
                    Path.home()
                    / ".cache/ms-playwright/chromium-*"
                    / "chrome-linux64"
                    / "chrome"
                )
            ),
            reverse=True,
        )
    )
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise FeedResolutionError("headless browser is not installed")


class FeedIdCache:
    def __init__(self, path: str | Path | None):
        self.path = Path(path).expanduser() if path else None
        self._data: dict[str, str] = {}
        self._load()

    def _load(self):
        if not self.path or not self.path.is_file():
            return
        try:
            value = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        if isinstance(value, dict):
            self._data = {
                str(chat_id): str(feed_id)
                for chat_id, feed_id in value.items()
                if isinstance(chat_id, str)
                and chat_id.startswith("oc_")
                and isinstance(feed_id, (str, int))
                and str(feed_id)
            }

    def get(self, chat_id: str) -> str:
        return self._data.get(chat_id, "")

    def delete(self, chat_id: str):
        if self._data.pop(chat_id, None) is not None:
            self._save()

    def _save(self):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self._data, ensure_ascii=False, indent=2))
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)
        os.chmod(self.path, 0o600)

    def put(self, chat_id: str, feed_id: str):
        self._data[chat_id] = str(feed_id)
        self._save()


class _CDP:
    def __init__(self, url: str):
        try:
            import websocket
        except ImportError as error:
            raise FeedResolutionError("websocket client is not installed") from error
        self.ws = websocket.create_connection(url, timeout=20)
        self._id = 0

    def call(self, method: str, params=None):
        self._id += 1
        request_id = self._id
        self.ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == request_id:
                if "error" in message:
                    raise FeedResolutionError("headless browser request failed")
                return message.get("result", {})

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_json(url: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=2, trust_env=False)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            time.sleep(0.2)
    raise FeedResolutionError("headless browser did not become ready")


def _evaluate(cdp: _CDP, expression: str, *, await_promise=False):
    result = cdp.call(
        "Runtime.evaluate",
        {
            "expression": expression,
            "awaitPromise": await_promise,
            "returnByValue": True,
        },
    )
    if result.get("exceptionDetails"):
        raise FeedResolutionError("headless browser evaluation failed")
    return result.get("result", {}).get("value")


class FeishuBrowserSession:
    """A reusable Web Messenger page for repeated feed-id lookups."""

    def __init__(self, *, web_url: str, cookies: list[dict], executable: str, timeout: float):
        self.web_url = web_url
        self.cookies = cookies
        self.executable = executable
        self.timeout = timeout
        self._profile = None
        self._process = None
        self._cdp = None
        self._started = False
        self._frozen = False

    def is_alive(self) -> bool:
        return bool(
            self._started
            and self._process is not None
            and self._process.poll() is None
            and self._cdp is not None
        )

    def start(self):
        if self._started:
            return
        binary = _find_chrome(self.executable)
        port = _free_port()
        self._profile = tempfile.TemporaryDirectory(prefix="vikingbot-feishu-")
        self._process = subprocess.Popen(
            [
                binary,
                "--headless=new",
                f"--remote-debugging-port={port}",
                "--remote-allow-origins=*",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-background-networking",
                f"--user-data-dir={self._profile.name}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _wait_json(f"http://127.0.0.1:{port}/json/version", self.timeout)
        response = httpx.put(
            f"http://127.0.0.1:{port}/json/new?{quote('about:blank', safe='')}",
            timeout=5,
            trust_env=False,
        )
        response.raise_for_status()
        page = response.json()
        self._cdp = _CDP(page["webSocketDebuggerUrl"])
        self._cdp.call("Network.enable")
        self._cdp.call("Page.enable")
        for cookie in self.cookies:
            if not isinstance(cookie, dict) or not cookie.get("name"):
                continue
            params = {
                "name": str(cookie.get("name")),
                "value": str(cookie.get("value", "")),
                "path": str(cookie.get("path") or "/"),
                "secure": bool(cookie.get("secure")),
                "httpOnly": bool(cookie.get("httpOnly")),
                "domain": str(cookie.get("domain") or ""),
            }
            if cookie.get("expires"):
                params["expires"] = float(cookie["expires"])
            if cookie.get("sameSite") in ("Strict", "Lax", "None"):
                params["sameSite"] = cookie["sameSite"]
            self._cdp.call("Network.setCookie", params)
        self._cdp.call("Page.navigate", {"url": self.web_url})

        deadline = time.monotonic() + self.timeout
        require_ready = False
        while time.monotonic() < deadline:
            try:
                require_ready = bool(
                    _evaluate(
                        self._cdp,
                        "Boolean(window.webpackChunkLarkletMessenger && "
                        "window.webpackChunkLarkletMessenger.length)",
                    )
                )
            except FeedResolutionError:
                require_ready = False
            if require_ready:
                break
            time.sleep(0.25)
        if not require_ready:
            raise FeedResolutionError("headless browser login or messenger load timed out")

        _evaluate(
            self._cdp,
            "(()=>{window.webpackChunkLarkletMessenger.push([['__ov_feed_probe__'],{}, "
            "function(r){window.__ov_feed_require=r}]);return true})()",
        )
        module_ready = False
        while time.monotonic() < deadline:
            try:
                module_ready = bool(
                    _evaluate(
                        self._cdp,
                        "Boolean(window.__ov_feed_require && window.__ov_feed_require.m && "
                        "window.__ov_feed_require.m[700426])",
                    )
                )
            except FeedResolutionError:
                module_ready = False
            if module_ready:
                break
            time.sleep(0.25)
        if not module_ready:
            raise FeedResolutionError("web messenger feed module did not load")
        # The first C$ call triggers the messenger feed sync.  Do it during
        # background warmup so the first real ticket does not pay this cost. A
        # newly loaded messenger can expose the module before its UI stores are
        # initialized; C$ then throws while reading an undefined module. Keep
        # the page active and retry until the same call used by resolve() works
        # before freezing it. Otherwise the cached browser stays permanently
        # half-initialized for the lifetime of the bot process.
        self._wait_for_feed_sync(deadline)
        self._started = True
        self._freeze()

    def _wait_for_feed_sync(self, deadline: float):
        while time.monotonic() < deadline:
            try:
                previews = _evaluate(
                    self._cdp,
                    "window.__ov_feed_require(700426).C$({filter:9,count:100})"
                    ".then(x=>x.previews||[])",
                    await_promise=True,
                )
            except FeedResolutionError:
                previews = None
            if isinstance(previews, list):
                return previews
            time.sleep(0.25)
        raise FeedResolutionError("web messenger feed sync did not become ready")

    def _freeze(self):
        if self._frozen or self._cdp is None:
            return
        try:
            self._cdp.call("Page.setWebLifecycleState", {"state": "frozen"})
        except FeedResolutionError:
            pass
        if self._process is not None and self._process.poll() is None:
            try:
                os.killpg(self._process.pid, signal.SIGSTOP)
            except ProcessLookupError:
                pass
        self._frozen = True

    def _activate(self):
        if not self._frozen or self._cdp is None:
            return
        if self._process is not None and self._process.poll() is None:
            try:
                os.killpg(self._process.pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
        try:
            self._cdp.call("Page.setWebLifecycleState", {"state": "active"})
        except FeedResolutionError:
            pass
        self._frozen = False
        time.sleep(0.1)

    def resolve(self, names: list[str]) -> str:
        if not self.is_alive():
            self.start()
        self._activate()
        expected = {_normal_name(name) for name in names if _normal_name(name)}
        if not expected:
            raise FeedResolutionError("chat has no usable name for feed resolution")

        # Try the Helpdesk filter first; most tickets are found there and
        # there is no reason to query the inbox/message views as well.
        for filter_id in (9, 1, 13):
            page = _evaluate(
                self._cdp,
                f"window.__ov_feed_require(700426).C$({{filter:{filter_id},count:100}})"
                ".then(x=>x.previews||[])",
                await_promise=True,
            )
            if not isinstance(page, list):
                continue
            matches = []
            for preview in page:
                if not isinstance(preview, dict):
                    continue
                feed_id = str(preview.get("feedId") or "")
                chat = preview.get("chatData") or {}
                if feed_id and _normal_name(str(chat.get("name") or "")) in expected:
                    matches.append(preview)
            if not matches:
                continue
            candidate_ids = [str(preview.get("feedId") or "") for preview in matches]
            validated = _evaluate(
                self._cdp,
                "window.__ov_feed_require(700426).hP({"
                f"feedIds:{json.dumps(candidate_ids)},syncDataStrategy:2"
                "}).then(x=>Object.keys(x.entityPreviews||{}))",
                await_promise=True,
            )
            if not isinstance(validated, list):
                continue
            valid_ids = set(validated)
            matches = [preview for preview in matches if str(preview.get("feedId")) in valid_ids]
            if not matches:
                continue
            matches.sort(
                key=lambda preview: (
                    int((preview.get("chatData") or {}).get("rankTime") or 0),
                    int(preview.get("updateTime") or 0),
                ),
                reverse=True,
            )
            feed_id = str(matches[0].get("feedId") or "")
            if feed_id:
                self._freeze()
                return feed_id
        self._freeze()
        raise FeedResolutionError("Helpdesk feed was not found in the web messenger")

    def close(self):
        if self._process is not None and self._process.poll() is None:
            try:
                os.killpg(self._process.pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
        self._started = False
        self._frozen = False
        if self._cdp is not None:
            self._cdp.close()
            self._cdp = None
        if self._process is not None and self._process.poll() is None:
            try:
                os.killpg(self._process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self._process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self._process.wait(timeout=5)
        self._process = None
        if self._profile is not None:
            self._profile.cleanup()
            self._profile = None


def _resolve_with_browser(
    *, web_url: str, cookies: list[dict], names: list[str], executable: str, timeout: float
) -> str:
    session = FeishuBrowserSession(
        web_url=web_url, cookies=cookies, executable=executable, timeout=timeout
    )
    try:
        session.start()
        return session.resolve(names)
    finally:
        session.close()


class FeishuWebFeedResolver:
    """Cache and, on a miss, resolve ``oc_*`` to the Web feed id."""

    def __init__(self, settings, *, api=None, cache_path=None, cookie_provider=None):
        self.settings = settings
        self.api = api
        self.cookie_provider = cookie_provider
        self.cache = FeedIdCache(cache_path)
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="feishu-feed"
        )
        self._browser_lock = asyncio.Lock()
        self._browser = None
        self._browser_cookie_signature = None

    def bind(self, *, api, cache_path=None, cookie_provider=None):
        self.api = api
        if cookie_provider is not None:
            self.cookie_provider = cookie_provider
        if cache_path is not None:
            self.cache = FeedIdCache(cache_path)

    async def _run_browser(self, method, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, method, *args)

    async def _ensure_browser(self, cookies):
        if not self.settings.web_url:
            raise FeedResolutionError("feishu web_url is not configured")
        signature = _cookie_signature(cookies)
        async with self._browser_lock:
            if (
                self._browser is not None
                and self._browser_cookie_signature == signature
                and self._browser.is_alive()
            ):
                return
            if self._browser is not None:
                await self._run_browser(self._browser.close)
            browser = FeishuBrowserSession(
                web_url=self.settings.web_url,
                cookies=cookies,
                executable=self.settings.browser_executable,
                timeout=self.settings.feed_resolution_timeout_seconds,
            )
            try:
                await self._run_browser(browser.start)
            except BaseException:
                await self._run_browser(browser.close)
                self._browser = None
                self._browser_cookie_signature = None
                raise
            self._browser = browser
            self._browser_cookie_signature = signature

    async def _discard_browser(self):
        async with self._browser_lock:
            browser = self._browser
            self._browser = None
            self._browser_cookie_signature = None
            if browser is not None:
                await self._run_browser(browser.close)

    async def warm(self, cookies=None):
        try:
            if cookies is None:
                if self.cookie_provider is None:
                    return
                cookies = await self.cookie_provider()
            await self._ensure_browser(cookies)
        except Exception:
            # Warmup is best effort; the normal resolve path reports the error.
            return

    async def close(self):
        async with self._browser_lock:
            if self._browser is not None:
                await self._run_browser(self._browser.close)
                self._browser = None
            self._executor.shutdown(wait=False)

    async def resolve(self, chat_id: str, *, cookies=None, force=False) -> str:
        if force:
            self.cache.delete(chat_id)
        else:
            configured = self.settings.feed_ids.get(chat_id)
            if configured:
                return str(configured)
            cached = self.cache.get(chat_id)
            if cached:
                return cached
        if not self.settings.auto_resolve_feed_id:
            raise FeedResolutionError("feishu internal feed id is not cached")
        if self.api is None:
            raise FeedResolutionError("headless feed resolver is not bound to Helpdesk")
        if not self.settings.web_url:
            raise FeedResolutionError("feishu web_url is not configured")
        if self.cookie_provider is None and cookies is None:
            raise FeedResolutionError("feishu web cookie provider is not configured")
        chat = await self.api.chat(chat_id)
        names = _chat_names(chat)
        if cookies is None:
            cookies = await self.cookie_provider()
        for attempt in range(2):
            await self._ensure_browser(cookies)
            try:
                feed_id = await self._run_browser(self._browser.resolve, names)
                break
            except FeedResolutionError:
                await self._discard_browser()
                if attempt:
                    raise
        self.cache.put(chat_id, feed_id)
        return feed_id


__all__ = [
    "FeedIdCache",
    "FeedResolutionError",
    "FeishuWebFeedResolver",
]
